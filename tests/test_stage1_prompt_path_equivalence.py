"""Offline prompt identity and authority-boundary controls."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
import socket

import pytest

from runtime.provider_runtime import hash_json
from services.prompt_registry import PromptRegistry
from services import stage1_reuse as reuse


REPO_ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("prompt identity controls must not use network or HTTP")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket, "getaddrinfo", reject)
    import requests

    monkeypatch.setattr(requests.Session, "send", reject)


@pytest.fixture()
def authorities(tmp_path):
    old_root = tmp_path / "original-worktree"
    (old_root / "prompts" / "active" / "stage1").mkdir(parents=True)
    (old_root / "prompts" / "registry.json").write_bytes(
        (REPO_ROOT / "prompts" / "registry.json").read_bytes()
    )
    for role in ("system", "user"):
        relative = Path("prompts") / "active" / "stage1" / f"{role}_analysis_v3.txt"
        (old_root / relative).write_bytes((REPO_ROOT / relative).read_bytes())
    return tuple(
        {
            role: registry.identity(f"stage1.analysis.{role}.v3").to_dict()
            for role in ("system", "user")
        }
        for registry in (PromptRegistry(old_root), PromptRegistry(REPO_ROOT))
    )


def binding(authority, **changes):
    user = authority["user"]
    payload = dict(
        canonical_paper_key="paper-a",
        source_mode="direct",
        source_pdf_content_sha256="1" * 64,
        stage1_extracted_text_hash="2" * 64,
        stage1_semantic_input_hash="3" * 64,
        preprocess_contract_hash="4" * 64,
        prompt_id=user["prompt_id"],
        prompt_version=user["prompt_version"],
        prompt_sha256=user["prompt_sha256"],
        prompt_template_hash=hash_json(authority),
        input_builder_policy_hash="5" * 64,
        summary_schema_hash="6" * 64,
        provider="Primary_Reader_API",
        model="fixture-reader",
        endpoint_type="chat_completions",
        provider_config_hash="7" * 64,
        visual_input_manifest_hash="8" * 64,
        visual_coverage_hash="9" * 64,
        visual_scan_schema_hash="a" * 64,
        extra={"prompt_authority": copy.deepcopy(authority)},
    )
    payload.update(changes)
    return reuse.Stage1ReusableSummaryBindingV1(**payload)


def test_real_prompt_registry_root_move_is_reusable_comparison(authorities):
    old, current = authorities
    left, right = binding(old), binding(current)
    frozen = (copy.deepcopy(left.to_dict()), copy.deepcopy(right.to_dict()))
    compared = left.compare(right)
    assert compared["equal"] is True
    assert compared["mismatches"] == {} and compared["missing_fields"] == []
    assert compared["original"]["prompt_template_hash"] != compared["current"]["prompt_template_hash"]
    assert compared["verified_prompt_path_equivalence"]["policy"] == "stage1-prompt-authority-root-equivalence/v1"
    assert (left.to_dict(), right.to_dict()) == frozen


def test_same_path_exact_behavior_is_unchanged(authorities):
    _, current = authorities
    exact = binding(current)
    # Fixed pre-equivalence structured comparison output contract. Do not
    # derive these fields/projections from the implementation being tested.
    expected_fields = [
        "canonical_paper_key",
        "source_mode",
        "source_pdf_content_sha256",
        "stage1_extracted_text_hash",
        "stage1_semantic_input_hash",
        "preprocess_contract_hash",
        "prompt_id",
        "prompt_version",
        "prompt_sha256",
        "prompt_template_hash",
        "input_builder_policy_hash",
        "provider",
        "model",
        "endpoint_type",
        "provider_config_hash",
        "summary_schema_hash",
        "visual_input_manifest_hash",
        "visual_coverage_hash",
        "visual_scan_schema_hash",
        "normalized_summary_payload_hash",
        "summary_payload_hash",
        "source_authority_job_id",
        "source_authority_registry_id",
        "source_authority_registry_revision",
        "source_authority_artifact_id",
        "source_authority_artifact_hash",
        "source_summary_manifest_id",
        "source_summary_manifest_hash",
        "source_provider_receipt_closure_id",
        "source_provider_receipt_closure_hash",
        "source_provider_receipt_ledger_id",
        "source_provider_receipt_ledger_hash",
    ]
    expected_projection = {
        "canonical_paper_key": "paper-a",
        "source_mode": "direct",
        "source_pdf_content_sha256": "1" * 64,
        "stage1_extracted_text_hash": "2" * 64,
        "stage1_semantic_input_hash": "3" * 64,
        "preprocess_contract_hash": "4" * 64,
        "prompt_id": current["user"]["prompt_id"],
        "prompt_version": current["user"]["prompt_version"],
        "prompt_sha256": current["user"]["prompt_sha256"],
        "prompt_template_hash": hash_json(current),
        "input_builder_policy_hash": "5" * 64,
        "provider": "Primary_Reader_API",
        "model": "fixture-reader",
        "endpoint_type": "chat_completions",
        "provider_config_hash": "7" * 64,
        "summary_schema_hash": "6" * 64,
        "visual_input_manifest_hash": "8" * 64,
        "visual_coverage_hash": "9" * 64,
        "visual_scan_schema_hash": "a" * 64,
        "normalized_summary_payload_hash": "",
        "summary_payload_hash": "",
        "source_authority_job_id": "",
        "source_authority_registry_id": "",
        "source_authority_registry_revision": "",
        "source_authority_artifact_id": "",
        "source_authority_artifact_hash": "",
        "source_summary_manifest_id": "",
        "source_summary_manifest_hash": "",
        "source_provider_receipt_closure_id": "",
        "source_provider_receipt_closure_hash": "",
        "source_provider_receipt_ledger_id": "",
        "source_provider_receipt_ledger_hash": "",
    }
    assert exact.compare(exact) == {
        "equal": True,
        "compared_fields": expected_fields,
        "missing_fields": [],
        "mismatches": {},
        "original": expected_projection,
        "current": expected_projection,
    }


@pytest.mark.parametrize("role,field,value", [
    ("system", "prompt_sha256", "b" * 64), ("user", "prompt_sha256", "c" * 64),
    ("system", "prompt_id", "foreign.system.v3"), ("user", "prompt_id", "foreign.user.v3"),
    ("system", "prompt_version", "v4"), ("user", "prompt_version", "v4"),
    ("system", "owner", "different-owner"), ("user", "status", "LEGACY"),
    ("system", "prompt_version", 1), ("user", "prompt_sha256", "not-a-sha"),
])
def test_changed_nonpath_identity_rejected_even_with_recomputed_hash(authorities, role, field, value):
    old, current = authorities
    modified = copy.deepcopy(current)
    modified[role][field] = value
    assert binding(old).compare(binding(modified))["equal"] is False


@pytest.mark.parametrize("change", [
    "role", "role-field", "missing-field", "missing-proof",
    "forged-old-hash", "forged-current-hash", "user-binding",
])
def test_unknown_missing_and_forged_proof_rejected(authorities, change):
    old, current = copy.deepcopy(authorities)
    if change == "role":
        old["critic"] = old["system"].copy()
        current["critic"] = current["system"].copy()
    if change == "role-field":
        old["system"]["future_execution_option"] = "same-unknown-value"
        current["system"]["future_execution_option"] = "same-unknown-value"
    if change == "missing-field":
        del old["system"]["owner"]
    left, right = binding(old), binding(current)
    if change == "missing-proof":
        right = replace(right, extra={})
    if change == "forged-old-hash":
        left = replace(left, prompt_template_hash="d" * 64)
    if change == "forged-current-hash":
        right = replace(right, prompt_template_hash="e" * 64)
    if change == "user-binding":
        right = replace(right, prompt_sha256="f" * 64)
    assert left.compare(right)["equal"] is False


@pytest.mark.parametrize("change", ["relative", "renamed", "split-root", "nul"])
def test_unsupported_path_structure_rejected(authorities, change):
    old, current = copy.deepcopy(authorities)
    if change == "relative":
        current["user"]["path"] = "prompts/active/stage1/user_analysis_v3.txt"
    if change == "renamed":
        current["user"]["path"] += ".other"
    if change == "split-root":
        current["user"]["path"] = old["user"]["path"]
    if change == "nul":
        current["user"]["path"] += "\x00"
    assert binding(old).compare(binding(current))["equal"] is False


@pytest.mark.parametrize("field,value", [
    ("model", "deepseek-flash"),
    ("provider_config_hash", "a95d9bcdb2c402abfbfcf746c1c4fcbbf7e8be0844724ef8426b3e0c2b57c817"),
])
def test_f1_real_model_and_config_differences_remain_rejected(authorities, field, value):
    old, current = authorities
    left = binding(old, model="deepseek-v4-flash-vision-exp",
        provider_config_hash="18d9439351a2aa99284ace3f2f1e483ecab76addd3237f9454c82db2b3514ba3")
    right = replace(binding(current, model=left.model, provider_config_hash=left.provider_config_hash), **{field: value})
    compared = left.compare(right)
    assert compared["equal"] is False and set(compared["mismatches"]) == {field}
    assert "verified_prompt_path_equivalence" in compared


def qualification():
    return reuse.Stage1VisualEvidenceQualificationV1(
        artifact_version="v2", require_complete_visual_coverage=True,
        scan_coverage_status="not_required", evidence_coverage_status="incomplete", final_synthesis_modality="text_only",
        final_raw_visual_recheck_status="not_run_fallback", visual_observation_artifact_version="v2",
        visual_scan_prompt_id="stage1.visual_scan.system.v3", visual_scan_prompt_version="v3", visual_scan_prompt_sha256="a" * 64,
        visual_scan_schema_hash="b" * 64, selection_mode="selective", selection_contract_version="stage1_visual_selection/v1",
        visual_selection_status="incomplete", required_visual_unit_count=1, required_visual_unit_ids=("table-1",),
        selected_visual_unit_ids=("table-1",), unresolved_visual_unit_ids=("table-1",),
        visual_extraction_strategy="direct_synthesis_visuals",
    ).to_dict()


def test_visual_semantic_rejection_is_unchanged_after_equivalent_compare(authorities):
    old, current = authorities
    left = binding(old, visual_evidence_qualification=qualification())
    right = binding(current, visual_evidence_qualification=qualification())
    assert left.compare(right)["equal"] is True
    assert reuse._verify_visual_evidence_qualification(left.visual_evidence_qualification) == (False, "prior_visual_coverage_incomplete")
    previous = {"paper_info": {"canonical_paper_key": "paper-a"}, "stage1_reuse": {"binding": left.to_dict()}}
    previous["stage1_reuse"]["binding"]["visual_evidence_qualification"] = {}
    result = reuse.evaluate_stage1_reuse(previous, right)
    assert not result.reusable and result.reason == "current_visual_evidence_qualification_missing"


def test_foreign_authority_is_not_created_by_equivalent_compare(authorities):
    old, current = authorities
    relaxed = reuse.Stage1VisualEvidenceQualificationV1(
        require_complete_visual_coverage=False,
        evidence_coverage_status="not_required", final_raw_visual_recheck_status="not_required",
    ).to_dict()
    left = binding(old, visual_evidence_qualification=relaxed,
        source_authority_job_id="foreign-owner", source_authority_artifact_id="foreign-summary")
    right = replace(binding(current, visual_evidence_qualification=relaxed),
        source_authority_job_id="", source_authority_artifact_id="")
    assert left.compare(right)["equal"] is True
    previous = {"paper_info": {"canonical_paper_key": "paper-a"}, "stage1_reuse": {"binding": left.to_dict()}}
    result = reuse.evaluate_stage1_reuse(previous, right)
    assert not result.reusable and result.reason == "source_authority_registry_resolver_missing"


def test_legacy_prompt_hash_has_no_path_fallback():
    left = reuse.Stage1ReusableSummaryBindingV1(prompt_hash="old")
    right = reuse.Stage1ReusableSummaryBindingV1(prompt_hash="new")
    assert left.compare(right)["equal"] is False


def test_typed_manifest_still_requires_exact_original_binding_hash(authorities):
    from tests.test_stage1_current_authority_boundaries import _typed_manifest_fixture

    payload, prior, previous = _typed_manifest_fixture()
    old, current = authorities
    original = replace(prior, prompt_id=old["user"]["prompt_id"],
        prompt_version=old["user"]["prompt_version"], prompt_sha256=old["user"]["prompt_sha256"],
        prompt_template_hash=hash_json(old), extra={"prompt_authority": old})
    moved = replace(original, prompt_template_hash=hash_json(current), extra={"prompt_authority": current})
    for field in ("prompt_id", "prompt_version", "prompt_sha256", "prompt_template_hash"):
        payload[field] = getattr(original, field)
    payload["binding"] = original.to_dict()
    payload["binding_hash"] = reuse.build_binding_hash(payload["binding"])
    payload["manifest_content_hash"] = hash_json({**payload, "manifest_content_hash": ""})
    frozen_payload = copy.deepcopy(payload)
    assert original.compare(moved)["equal"] is True
    verified, reason = reuse._validate_manifest_self_binding(payload, binding=original, previous_summary=previous)
    assert verified is not None, reason
    rejected, reason = reuse._validate_manifest_self_binding(payload, binding=moved, previous_summary=previous)
    assert rejected is None and reason == "typed_manifest_imported_binding_mismatch"
    assert payload == frozen_payload


def test_content_tamper_is_rejected_by_real_prompt_registry(authorities):
    old, _ = authorities
    target = Path(old["user"]["path"])
    target.write_bytes(target.read_bytes() + b"\nchanged content\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        PromptRegistry(target.parents[3]).identity("stage1.analysis.user.v3")

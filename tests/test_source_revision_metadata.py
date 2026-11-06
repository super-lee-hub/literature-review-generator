from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.provider_runtime import hash_json

CONTRACT = "source-closed-provider/v2"


def _spec(tmp_path: Path) -> RuntimeJobSpec:
    return RuntimeJobSpec(
        project_name="source-revision-contract", job_id="explicit-child",
        source=RuntimeSourceSpec(mode="direct", pdf_folder=str(tmp_path)),
        config=str(tmp_path / "config.ini"), workspace_path=str(tmp_path / "child"),
    )


def _ref(tmp_path: Path) -> dict:
    return {"schema_version": "source-revision-plan-ref/v1", "path": str(tmp_path / "plan.json"),
            "file_sha256": "1" * 64, "content_hash": "2" * 64}


def test_successor_metadata_opt_in_preserves_exact_roundtrip(tmp_path):
    original = replace(_spec(tmp_path), metadata={"provider_semantic_contract_version": CONTRACT})
    payload = original.to_dict()
    parsed = RuntimeJobSpec.from_mapping(payload)
    assert parsed.to_dict() == payload
    assert hash_json(parsed.to_dict()) == hash_json(payload)


def test_legacy_spec_roundtrip_does_not_inject_successor_defaults(tmp_path):
    payload = _spec(tmp_path).to_dict()
    before = deepcopy(payload)
    parsed = RuntimeJobSpec.from_mapping(payload)
    assert parsed.to_dict() == before
    assert hash_json(parsed.to_dict()) == hash_json(before)
    assert "provider_semantic_contract_version" not in parsed.metadata
    assert "source_revision_plan" not in parsed.metadata


def test_derived_plan_reference_requires_explicit_successor_contract(tmp_path):
    with pytest.raises(ValueError):
        replace(_spec(tmp_path), metadata={"source_revision_plan": _ref(tmp_path)}).validate()
    spec = replace(_spec(tmp_path), metadata={
        "provider_semantic_contract_version": CONTRACT, "source_revision_plan": _ref(tmp_path),
    })
    spec.validate()
    assert RuntimeJobSpec.from_mapping(spec.to_dict()).to_dict() == spec.to_dict()


@pytest.mark.parametrize("version", [None, True, 2, "v2", "legacy/v1", CONTRACT + " "])
def test_unknown_or_coerced_contract_versions_reject(tmp_path, version):
    with pytest.raises(ValueError):
        replace(_spec(tmp_path), metadata={"provider_semantic_contract_version": version}).validate()


@pytest.mark.parametrize("mutation", ["missing", "extra", "relative", "bad_file_hash", "bad_content_hash"])
def test_plan_reference_is_closed_and_requires_absolute_path(tmp_path, mutation):
    ref = _ref(tmp_path)
    if mutation == "missing":
        del ref["content_hash"]
    elif mutation == "extra":
        ref["trusted"] = True
    elif mutation == "relative":
        ref["path"] = "plan.json"
    elif mutation == "bad_file_hash":
        ref["file_sha256"] = "not-a-file-sha"
    else:
        ref["content_hash"] = 123
    with pytest.raises(ValueError):
        replace(_spec(tmp_path), metadata={
            "provider_semantic_contract_version": CONTRACT, "source_revision_plan": ref,
        }).validate()


def test_child_core_hash_removes_only_plan_reference_without_mutating_inputs(tmp_path):
    from runtime.source_revision import child_core_spec_hash, child_core_spec_payload

    base = replace(_spec(tmp_path), metadata={
        "provider_semantic_contract_version": CONTRACT,
        "provider_budget": {"max_provider_calls_total": 10},
        "review_intent": {"question": "retain source-specific qualifiers"},
    }).to_dict()
    referenced = deepcopy(base)
    referenced["metadata"]["source_revision_plan"] = _ref(tmp_path)
    before = deepcopy(referenced)
    assert child_core_spec_payload(referenced) == base
    assert referenced == before
    assert child_core_spec_hash(referenced) == hash_json(base)
    assert hash_json(referenced) != child_core_spec_hash(referenced)
    for field, value in ("provider_budget", {"max_provider_calls_total": 11}), (
        "review_intent", {"question": "omit source-specific qualifiers"},
    ):
        changed = deepcopy(referenced)
        changed["metadata"][field] = value
        assert child_core_spec_hash(changed) != child_core_spec_hash(referenced)
    changed_source = deepcopy(referenced)
    changed_source["source"]["pdf_folder"] = str(tmp_path / "different-corpus")
    assert child_core_spec_hash(changed_source) != child_core_spec_hash(referenced)


def _pdf_rows(tmp_path):
    return [{"canonical_paper_key": key, "source_pdf": str(tmp_path / (key + ".pdf")),
             "pdf_sha256": digit * 64} for key, digit in (("paper-a", "a"), ("paper-b", "b"))]


def test_single_pdf_delta_uses_actual_byte_identity_not_path(tmp_path):
    from runtime.source_revision import classify_replacement_delta

    old = _pdf_rows(tmp_path)
    new = deepcopy(old)
    new[0]["source_pdf"] = str(tmp_path / "relocated-a.pdf")
    new[1]["source_pdf"] = str(tmp_path / "revised-b.pdf")
    new[1]["pdf_sha256"] = "c" * 64
    delta = classify_replacement_delta(old, new)
    assert delta.changed_paper_key == "paper-b"
    assert delta.unchanged_paper_keys == ("paper-a",)
    assert old == _pdf_rows(tmp_path)


@pytest.mark.parametrize("operation", ["no_change", "path_only", "two_changes", "reorder", "add", "remove", "rename", "duplicate"])
def test_unsupported_first_mode_operations_reject_explicitly(tmp_path, operation):
    from runtime.source_revision import classify_replacement_delta

    old = _pdf_rows(tmp_path)
    new = deepcopy(old)
    new[1]["pdf_sha256"] = "c" * 64
    if operation == "no_change":
        new = deepcopy(old)
    elif operation == "path_only":
        new = deepcopy(old)
        new[0]["source_pdf"] = str(tmp_path / "moved-a.pdf")
    elif operation == "two_changes":
        new[0]["pdf_sha256"] = "d" * 64
    elif operation == "reorder":
        new.reverse()
    elif operation == "add":
        new.append({**old[0], "canonical_paper_key": "paper-c"})
    elif operation == "remove":
        new.pop()
    elif operation == "rename":
        new[0]["canonical_paper_key"] = "renamed-a"
    else:
        new[1]["canonical_paper_key"] = "paper-a"
    with pytest.raises(ValueError):
        classify_replacement_delta(old, new)

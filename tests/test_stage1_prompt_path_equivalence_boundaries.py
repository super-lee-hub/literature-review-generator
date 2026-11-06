"""Pure prompt identity boundaries using canonical product imports."""
from __future__ import annotations

import builtins
import copy
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.provider_runtime import hash_json
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    _prompt_path_equivalence,
)


def _authority(root: str, *, windows: bool = False) -> dict[str, dict[str, str]]:
    separator = "\\" if windows else "/"
    return {
        role: {
            "prompt_id": f"stage1.analysis.{role}.v3",
            "prompt_version": "v3",
            "status": "ACTIVE",
            "owner": "independent-pure-control",
            "path": root + separator + separator.join(
                ("prompts", "active", "stage1", f"{role}_analysis_v3.txt")
            ),
            "prompt_sha256": ("a" if role == "system" else "b") * 64,
        }
        for role in ("system", "user")
    }


def _binding(authority: dict[str, dict[str, str]]) -> Stage1ReusableSummaryBindingV1:
    user = authority["user"]
    return Stage1ReusableSummaryBindingV1(
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


@pytest.mark.parametrize(
    "left,right",
    [
        (_authority(r"D:\old owner", windows=True), _authority(r"E:\new owner", windows=True)),
        (_authority("/old-owner"), _authority("/new-owner")),
        (
            _authority(r"\\host\share\old", windows=True),
            _authority(r"\\host\share\new", windows=True),
        ),
        (_authority("/old-posix"), _authority(r"E:\new-windows", windows=True)),
    ],
)
def test_absolute_flavours_have_path_only_proof(left, right):
    before = (copy.deepcopy(left), copy.deepcopy(right))
    original, current = _binding(left), _binding(right)
    assert _prompt_path_equivalence(original, current) is not None
    assert original.compare(current)["equal"] is True
    assert (left, right) == before


@pytest.mark.parametrize(
    "location",
    [
        r"D:relative\prompts\active\stage1\user_analysis_v3.txt",
        r"\rooted-without-drive\prompts\active\stage1\user_analysis_v3.txt",
        "../prompts/active/stage1/user_analysis_v3.txt",
        "https://host/prompts/active/stage1/user_analysis_v3.txt",
        "/new/prompts/active/stage1/user_analysis_v3.txt:other-stream",
        "/new/prompts/active/stage1/user_analysis_v3.txt\x00",
    ],
)
def test_unsupported_path_cannot_get_helper_proof(location):
    left, right = _authority("/old"), _authority("/new")
    right["user"]["path"] = location
    assert _prompt_path_equivalence(_binding(left), _binding(right)) is None


@pytest.mark.parametrize("role", ["system", "user"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_id", "foreign.prompt"),
        ("prompt_version", "v4"),
        ("status", "LEGACY"),
        ("owner", "foreign-owner"),
        ("prompt_sha256", "c" * 64),
    ],
)
def test_each_nonpath_identity_fails_direct_proof(role, field, value):
    left, right = _authority("/old"), _authority("/new")
    right[role][field] = value
    # Both complete hashes are recomputed. Call the proof directly so other
    # comparison fields cannot mask a prompt-identity verification gap.
    assert _prompt_path_equivalence(_binding(left), _binding(right)) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt_id", "foreign.user"),
        ("prompt_version", "v4"),
        ("prompt_sha256", "d" * 64),
    ],
)
def test_user_top_level_identity_is_not_supplied_by_extra(field, value):
    original, current = _binding(_authority("/old")), _binding(_authority("/new"))
    forged = replace(current, **{field: value})
    assert _prompt_path_equivalence(original, forged) is None


def test_two_forged_captures_do_not_inherit_original_complete_hashes():
    original, current = _binding(_authority("/old")), _binding(_authority("/new"))
    original.extra["prompt_authority"]["system"]["owner"] = "same-forged-owner"
    current.extra["prompt_authority"]["system"]["owner"] = "same-forged-owner"
    assert _prompt_path_equivalence(original, current) is None


def test_path_proof_does_not_resolve_or_read_locators(monkeypatch):
    original = _binding(_authority(r"D:\unavailable-owner", windows=True))
    current = _binding(_authority("/unavailable-current"))

    def denied(*_args, **_kwargs):
        raise AssertionError("path-equivalence proof performed filesystem I/O")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", denied)
        patch.setattr(Path, "resolve", denied)
        patch.setattr(Path, "stat", denied)
        patch.setattr(Path, "read_bytes", denied)
        proof = _prompt_path_equivalence(original, current)
    assert proof is not None

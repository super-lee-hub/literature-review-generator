from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import runtime.export_bundle as export_bundle_module
import runtime.runner as runner_module
from runtime.export_bundle import _hash, _resolve_export_validation_policy
from runtime.provider_runtime import hash_json
from runtime.stage_planning import build_stage_plan
from services.artifact_registry import ArtifactRecord, ArtifactRegistry, RegistryError
from services.job_workspace import JobWorkspace


def _fixture(
    tmp_path: Path,
    *,
    validation_enabled: bool = True,
    validation_required: bool | None = None,
    require_clean_validation: bool | None = None,
    allow_unvalidated_when_validation_optional: bool | None = None,
    include_stage_plan: bool = True,
    metadata_overrides: dict[str, Any] | None = None,
    payload_overrides: dict[str, Any] | None = None,
) -> tuple[ArtifactRegistry, list[ArtifactRecord], str, dict[str, Any]]:
    job_id = "job-export-policy"
    registry = ArtifactRegistry(tmp_path / "registry.json", job_id)
    plan = build_stage_plan(
        action="run_all",
        requested_stages=None,
        validation_enabled=validation_enabled,
        validation_required=validation_required,
        require_clean_validation=require_clean_validation,
        allow_unvalidated_when_validation_optional=allow_unvalidated_when_validation_optional,
    ).to_dict()
    metadata: dict[str, Any] = {"stage_plan": plan} if include_stage_plan else {}
    if metadata_overrides:
        metadata.update(metadata_overrides)
    payload: dict[str, Any] = {
        "project_name": "policy-test",
        "source": {"mode": "pdf_folder", "pdf_folder": str(tmp_path)},
        "job_id": job_id,
        "action": "run_all",
        "metadata": metadata,
        "artifact_type": "runtime_job_spec",
        "artifact_version": "v1",
    }
    if payload_overrides:
        payload.update(payload_overrides)
    path = tmp_path / "runtime_job_spec_v1.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    registry.register_file(
        artifact_id="runtime_job_spec",
        artifact_role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=path,
        producer="tests.export_validation_policy",
    )
    _, snapshot = registry.capture_record_snapshot()
    return registry, list(snapshot), _hash(payload), plan


def test_required_clean_policy_is_projected_from_runtime_stage_plan(tmp_path: Path) -> None:
    registry, records, spec_hash, plan = _fixture(tmp_path)
    policy = _resolve_export_validation_policy(
        registry,
        records_snapshot=records,
        spec_hash=spec_hash,
        closure={"status": "clean"},
    )
    assert policy == {
        "projection_version": "export-validation-policy-v1",
        "source": "runtime_stage_plan",
        "runtime_spec_hash": records[0].content_hash,
        "stage_plan_hash": hash_json(plan),
        "validation_enabled": True,
        "validation_required": True,
        "require_clean_validation": True,
        "allow_unvalidated_when_validation_optional": False,
    }


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "validation_enabled": True,
                "validation_required": False,
                "require_clean_validation": False,
                "allow_unvalidated_when_validation_optional": True,
            },
            (True, False, False, True),
        ),
        (
            {
                "validation_enabled": False,
                "validation_required": False,
                "require_clean_validation": False,
                "allow_unvalidated_when_validation_optional": True,
            },
            (False, False, False, True),
        ),
    ],
)
def test_optional_and_not_requested_policy_flags_are_not_inferred_from_closure(
    tmp_path: Path, kwargs: dict[str, Any], expected: tuple[bool, bool, bool, bool]
) -> None:
    registry, records, spec_hash, _ = _fixture(tmp_path, **kwargs)
    policy = _resolve_export_validation_policy(
        registry,
        records_snapshot=records,
        spec_hash=spec_hash,
        closure={"status": "clean", "validation_required": True},
    )
    assert tuple(policy[field] for field in (
        "validation_enabled",
        "validation_required",
        "require_clean_validation",
        "allow_unvalidated_when_validation_optional",
    )) == expected


def test_only_absent_stage_plan_uses_legacy_closure_projection(tmp_path: Path) -> None:
    registry, records, spec_hash, _ = _fixture(
        tmp_path,
        include_stage_plan=False,
    )
    policy = _resolve_export_validation_policy(
        registry,
        records_snapshot=records,
        spec_hash=spec_hash,
        closure={
            "status": "clean",
            "validation_required": True,
            "semantic": {
                "validation_enabled": True,
                "validation_required": True,
                "require_clean_validation": True,
                "allow_unvalidated": False,
                "stage_plan_hash": "legacy-stage-plan",
            },
        },
    )
    assert policy["source"] == "legacy_validation_closure"
    assert policy["validation_required"] is True
    assert policy["stage_plan_hash"] == "legacy-stage-plan"


def test_legacy_projection_requires_no_runtime_spec_and_empty_hash(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry.json", "job-export-policy")
    policy = _resolve_export_validation_policy(
        registry,
        records_snapshot=(),
        spec_hash="",
        closure={
            "status": "clean",
        },
    )
    assert policy["source"] == "legacy_validation_closure"
    assert policy["validation_required"] is False
    assert policy["validation_enabled"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda plan: {**plan, "validation_enabled": "true"},
        lambda plan: {**plan, "validation_required": None},
        lambda plan: {**plan, "requested_stages": None},
        lambda plan: {**plan, "action": "validate_review"},
        lambda plan: {**plan, "required_stages": ["source_intake"]},
    ],
)
def test_present_malformed_or_inconsistent_stage_plan_fails_closed(
    tmp_path: Path, mutator,
) -> None:
    _, _, _, plan = _fixture(tmp_path)
    registry, records, spec_hash, _ = _fixture(
        tmp_path / "nested",
        metadata_overrides={"stage_plan": mutator(plan)},
    )
    with pytest.raises(RegistryError, match="stage plan"):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


@pytest.mark.parametrize(
    "stage_plan",
    [None, "stage-plan", {}, {"version": "stage-plan-v1", "action": "run_all"}],
)
def test_stage_plan_null_string_empty_or_missing_fields_fails_closed(
    tmp_path: Path, stage_plan: Any,
) -> None:
    registry, records, spec_hash, _ = _fixture(
        tmp_path,
        metadata_overrides={"stage_plan": stage_plan},
    )
    with pytest.raises(RegistryError, match="stage plan"):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


@pytest.mark.parametrize("shadow", [
    {"requested_stages": ["outline"]},
    {"required_stages": ["source_intake"]},
    {"validation_required": False},
])
def test_metadata_shadow_flags_must_match_stage_plan(tmp_path: Path, shadow: dict[str, Any]) -> None:
    registry, records, spec_hash, _ = _fixture(
        tmp_path,
        metadata_overrides=shadow,
    )
    with pytest.raises(RegistryError, match="metadata|stage plan"):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


@pytest.mark.parametrize(
    ("record_mutation", "payload_overrides"),
    [
        ("hash", {}),
        (None, {"job_id": "foreign-job"}),
        (None, {"action": "validate_review"}),
    ],
)
def test_runtime_spec_identity_and_bytes_are_bound(
    tmp_path: Path, record_mutation: str | None, payload_overrides: dict[str, Any]
) -> None:
    registry, records, spec_hash, _ = _fixture(
        tmp_path,
        payload_overrides=payload_overrides,
    )
    if record_mutation == "hash":
        records = [replace(records[0], content_hash="0" * 64)]
    with pytest.raises(RegistryError):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


def test_runtime_spec_real_bytes_tamper_is_rejected(tmp_path: Path) -> None:
    registry, records, spec_hash, _ = _fixture(tmp_path)
    spec_path = Path(records[0].path)
    spec_path.write_bytes(spec_path.read_bytes() + b"\n tampered")
    with pytest.raises(RegistryError):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


@pytest.mark.parametrize(
    "field", ["job_id", "artifact_role", "artifact_type", "artifact_version", "status"]
)
def test_runtime_spec_record_identity_must_be_local_ready_v1(
    tmp_path: Path, field: str,
) -> None:
    registry, records, spec_hash, _ = _fixture(tmp_path)
    value: Any = {
        "job_id": "foreign-job",
        "artifact_role": "runtime_job_spec",
        "artifact_type": "other",
        "artifact_version": "v2",
        "status": "quarantined",
    }[field]
    records = [replace(records[0], **{field: value})]
    with pytest.raises(RegistryError):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=records,
            spec_hash=spec_hash,
            closure={"status": "clean"},
        )


def test_missing_runtime_spec_with_nonempty_hash_is_rejected(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "registry.json", "job-export-policy")
    with pytest.raises(RegistryError):
        _resolve_export_validation_policy(
            registry,
            records_snapshot=(),
            spec_hash="f" * 64,
            closure={"status": "clean"},
        )


def test_derive_fences_policy_against_registry_snapshot_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = JobWorkspace.create(str(tmp_path), "policy", "job-export-policy")
    registry = ArtifactRegistry(workspace.paths.registry_path, workspace.job_id)
    plan = build_stage_plan(
        action="run_all", requested_stages=None, validation_enabled=True
    ).to_dict()
    payload = {
        "project_name": "policy-test",
        "source": {"mode": "pdf_folder", "pdf_folder": str(tmp_path)},
        "job_id": workspace.job_id,
        "action": "run_all",
        "metadata": {"stage_plan": plan},
        "artifact_type": "runtime_job_spec",
        "artifact_version": "v1",
    }
    spec_path = Path(workspace.artifact_path("runtime_job_spec_v1.json"))
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    registry.register_file(
        artifact_id="runtime_job_spec",
        artifact_role="runtime_spec",
        artifact_type="runtime_job_spec",
        artifact_version="v1",
        path=spec_path,
        producer="tests.export_validation_policy",
    )
    revision, snapshot = registry.capture_record_snapshot()
    initial_hash = snapshot[0].content_hash
    stage_map = SimpleNamespace(
        stages={},
        blocking_issues=[],
        requested_stages=tuple(plan["requested_stages"]),
        to_dict=lambda: {
            "artifact_type": "current_stage_closure_map",
            "artifact_version": "v1",
            "job_id": workspace.job_id,
            "requested_stages": list(plan["requested_stages"]),
            "spec_hash": _hash(payload),
            "stages": {},
            "provider_closures_by_stage": {},
            "blocking_issues": [],
        },
    )
    monkeypatch.setattr(
        runner_module.AgentRuntimeRunner,
        "status",
        lambda _root: SimpleNamespace(
            job_status="completed",
            completion_status="complete",
            canonical_ready=True,
            completion_reasons=(),
            completion_evidence_hash="completion",
        ),
        raising=False,
    )
    import validation.closure as closure_module

    monkeypatch.setattr(
        closure_module.ValidationClosureService,
        "inspect",
        lambda _self: SimpleNamespace(
            status="clean", semantic={}, to_dict=lambda: {"status": "clean", "semantic": {}}
        ),
    )
    monkeypatch.setattr(
        closure_module,
        "resolve_current_stage_closure_map",
        lambda _registry, **_kwargs: stage_map,
    )

    replacement = Path(workspace.artifact_path("runtime_job_spec_replacement.json"))
    replacement.write_text(json.dumps({**payload, "project_name": "replaced"}, sort_keys=True), encoding="utf-8")
    original_policy_resolver = export_bundle_module._resolve_export_validation_policy

    def resolve_then_replace(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_policy_resolver(*args, **kwargs)
        registry.register_file(
            artifact_id="runtime_job_spec",
            artifact_role="runtime_spec",
            artifact_type="runtime_job_spec",
            artifact_version="v1",
            path=replacement,
            producer="tests.export_validation_policy",
        )
        return result

    monkeypatch.setattr(export_bundle_module, "_resolve_export_validation_policy", resolve_then_replace)
    evidence = export_bundle_module._derive_current_evidence(
        workspace,
        registry,
        records_snapshot=snapshot,
        expected_snapshot_revision=revision,
    )
    assert any(issue.startswith("dependency_closure:") for issue in evidence["issues"])
    assert evidence["validation_policy"]["source"] in {"unavailable", "runtime_stage_plan"}
    assert initial_hash != registry.get("runtime_job_spec").content_hash

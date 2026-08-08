from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Iterable

from runtime.job_spec import RuntimeJobSpec, RuntimeSourceSpec
from runtime.outline_v3_replay import MODEL_REPLAY_ARTIFACT_TYPE
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactRegistry, file_sha256
from services.job_runner import build_job_request_from_mapping
from services.queue_service import QueueJobSpec
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    _authority_summary_matches,
    evaluate_stage1_reuse,
)
from validation.closure import zero_call_evidence_policy


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_POLICY_FIELDS = frozenset(
    {
        "validation_required",
        "require_clean_validation",
        "allow_unvalidated_when_validation_optional",
    }
)


def _tree(relative_path: str) -> ast.Module:
    path = REPO_ROOT / relative_path
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _definition(tree: ast.AST, *qualified_name: str) -> ast.AST:
    scope = tree
    for name in qualified_name:
        body = getattr(scope, "body", ())
        match = next(
            (
                item
                for item in body
                if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == name
            ),
            None,
        )
        assert match is not None, f"missing executable definition: {'.'.join(qualified_name)}"
        scope = match
    return scope


def _assigned_literal(tree: ast.AST, target_name: str) -> Any:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == target_name for target in targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing literal assignment: {target_name}")


def _call_name(node: ast.Call) -> str:
    parts: list[str] = []
    value: ast.AST = node.func
    while isinstance(value, ast.Attribute):
        parts.append(value.attr)
        value = value.value
    if isinstance(value, ast.Name):
        parts.append(value.id)
    return ".".join(reversed(parts))


def _calls(node: ast.AST) -> list[ast.Call]:
    return [item for item in ast.walk(node) if isinstance(item, ast.Call)]


def _identifier_names(node: ast.AST) -> set[str]:
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    names.update(item.attr for item in ast.walk(node) if isinstance(item, ast.Attribute))
    return names


def _string_literals(node: ast.AST) -> set[str]:
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _target_names(item)}
    return set()


def _subscript_key(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Subscript):
        return None
    key = node.slice
    return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def _function_call_names(tree: ast.AST, *qualified_name: str) -> set[str]:
    return {_call_name(call) for call in _calls(_definition(tree, *qualified_name))}


def _argument_names(definition: ast.AST) -> set[str]:
    assert isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
    return {
        argument.arg
        for argument in (
            *definition.args.posonlyargs,
            *definition.args.args,
            *definition.args.kwonlyargs,
        )
    }


def _bool_calls_touching_policy(node: ast.AST) -> list[ast.Call]:
    return [
        call
        for call in _calls(node)
        if _call_name(call) == "bool"
        and VALIDATION_POLICY_FIELDS.intersection(_string_literals(call))
    ]


def _defaulted_policy_gets(node: ast.AST) -> list[ast.Call]:
    findings: list[ast.Call] = []
    for call in _calls(node):
        if not _call_name(call).endswith(".get") or len(call.args) < 2:
            continue
        key = call.args[0]
        if isinstance(key, ast.Constant) and key.value in VALIDATION_POLICY_FIELDS:
            findings.append(call)
    return findings


def _resolved_keyword_strings(
    module_tree: ast.Module,
    definition: ast.AST,
    keyword_name: str,
) -> set[str]:
    constants: dict[str, str] = {}
    for node in module_tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    values: set[str] = set()
    for call in _calls(definition):
        for keyword in call.keywords:
            if keyword.arg != keyword_name:
                continue
            if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                values.add(keyword.value.value)
            elif isinstance(keyword.value, ast.Name) and keyword.value.id in constants:
                values.add(constants[keyword.value.id])
    return values


def _binding(**overrides: Any) -> Stage1ReusableSummaryBindingV1:
    values: dict[str, Any] = {
        "canonical_paper_key": "10.1000/guard",
        "source_mode": "direct",
        "source_pdf_content_sha256": "a" * 64,
        "stage1_extracted_text_hash": "b" * 64,
        "stage1_semantic_input_hash": "c" * 64,
        "preprocess_contract_hash": "d" * 64,
        "prompt_template_hash": "e" * 64,
        "input_builder_policy_hash": "f" * 64,
        "provider_config_hash": "1" * 64,
        "summary_schema_hash": "2" * 64,
        "visual_input_manifest_hash": "3" * 64,
    }
    values.update(overrides)
    return Stage1ReusableSummaryBindingV1(**values)


def _write_summary_authority(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    "paper_info": {"canonical_paper_key": "10.1000/guard"},
                    "ai_summary": summary,
                    "summary_payload_hash": hash_json(summary),
                }
            ]
        ),
        encoding="utf-8",
    )


def test_architecture_guard_pdf_byte_hash_never_receives_semantic_input_hash() -> None:
    service_tree = _tree("services/stage1_analysis_service.py")
    semantic_names = {
        "semantic_source_hash",
        "stage1_semantic_input_hash",
        "stage1_extracted_text_hash",
        "source_pdf_hash",
        "preprocess_hash",
    }
    pdf_hash_fields = {
        "source_pdf_content_sha256",
        "source_pdf_content_hash",
        "pdf_content_hash",
        "source_pdf_file_hash",
    }

    byte_hash_assignments: list[ast.AST] = []
    for relative_path in (
        "services/stage1_analysis_service.py",
        "services/stage1_reuse.py",
        "runtime/orchestrator.py",
    ):
        module_tree = _tree(relative_path)
        for node in ast.walk(module_tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets: Iterable[ast.AST] = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                target_fields = {
                    field
                    for target in targets
                    for field in (*_target_names(target), _subscript_key(target))
                    if field is not None
                }
                if pdf_hash_fields.intersection(target_fields):
                    byte_hash_assignments.append(node.value)
                    assert not semantic_names.intersection(_identifier_names(node.value)), (
                        f"PDF byte hash assignment uses semantic identity in {relative_path}:"
                        f"{node.lineno}"
                    )
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in pdf_hash_fields
                    ):
                        assert not semantic_names.intersection(_identifier_names(value)), (
                            f"PDF byte hash mapping uses semantic identity in {relative_path}:"
                            f"{value.lineno}"
                        )
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in pdf_hash_fields:
                        assert not semantic_names.intersection(_identifier_names(keyword.value)), (
                            f"{keyword.arg} is sourced from semantic/preprocess identity in "
                            f"{relative_path}:{keyword.value.lineno}"
                        )

    assert any(
        isinstance(value, ast.Call)
        and _call_name(value).endswith("file_sha256")
        and "source_pdf" in _identifier_names(value)
        for value in byte_hash_assignments
    ), "source_pdf_content_sha256 must originate from file_sha256(source_pdf)"

    reuse_tree = _tree("services/stage1_reuse.py")
    from_mapping = _definition(reuse_tree, "Stage1ReusableSummaryBindingV1", "from_mapping")
    aliases = _assigned_literal(from_mapping, "aliases")
    assert semantic_names.isdisjoint(set(aliases["source_pdf_content_sha256"]))


def test_architecture_guard_direct_paths_are_metadata_not_exact_equality_inputs() -> None:
    structured_fields = set(
        _assigned_literal(_tree("services/stage1_reuse.py"), "_STRUCTURED_COMPARISON_FIELDS")
    )
    assert {
        "source_pdf_content_sha256",
        "stage1_semantic_input_hash",
    }.issubset(structured_fields)
    assert structured_fields.isdisjoint(
        {
            "source_paper_id",
            "source_pdf",
            "source_pdf_path",
            "original_source_location",
            "current_source_location",
            "location_changed",
        }
    )

    original = _binding(
        source_paper_id=r"D:\papers\a.pdf",
        source_pdf=r"D:\papers\a.pdf",
        original_source_location=r"D:\papers\a.pdf",
        current_source_location=r"D:\papers\a.pdf",
    )
    moved = _binding(
        source_paper_id=r"E:\library\a.pdf",
        source_pdf=r"E:\library\a.pdf",
        original_source_location=r"D:\papers\a.pdf",
        current_source_location=r"E:\library\a.pdf",
        location_changed=True,
    )

    assert original.compare(moved)["equal"] is True


def test_architecture_guard_rejects_path_only_summary_authority(tmp_path: Path) -> None:
    reuse_tree = _tree("services/stage1_reuse.py")
    assert "_registered_source_is_verifiable" in _function_call_names(
        reuse_tree,
        "evaluate_stage1_reuse",
    )
    summary = {"summary": "registered bytes"}
    authority_path = tmp_path / "summary.json"
    _write_summary_authority(authority_path, summary)
    binding = _binding(
        normalized_summary_payload_hash=hash_json(summary),
        summary_payload_hash=hash_json(summary),
        source_authority_job_id="parent-job",
        source_authority_artifact_hash=file_sha256(authority_path),
        source_authority_artifact_path=str(authority_path),
        registered_source_artifact_hash=file_sha256(authority_path),
        registered_source_artifact_path=str(authority_path),
    )

    result = evaluate_stage1_reuse(
        {
            "paper_info": {"canonical_paper_key": "10.1000/guard"},
            "ai_summary": summary,
            "stage1_reuse": {"binding": binding.to_dict()},
        },
        binding,
        registry=None,
    )

    assert result.reusable is False
    assert result.reason == "source_authority_artifact_id_missing"


def test_architecture_guard_current_snapshot_is_never_prior_authority(tmp_path: Path) -> None:
    evaluator = _definition(_tree("services/stage1_reuse.py"), "evaluate_stage1_reuse")
    authority_calls = [
        call
        for call in _calls(evaluator)
        if _call_name(call) == "_registered_source_is_verifiable"
    ]
    assert len(authority_calls) == 1
    assert isinstance(authority_calls[0].args[0], ast.Name)
    assert authority_calls[0].args[0].id == "original"

    snapshot = tmp_path / "current-snapshot.json"
    summary = {"summary": "derived copy"}
    _write_summary_authority(snapshot, summary)
    binding = _binding(
        normalized_summary_payload_hash=hash_json(summary),
        current_snapshot_artifact_id="child:snapshot",
        current_snapshot_artifact_hash=file_sha256(snapshot),
        current_snapshot_artifact_path=str(snapshot),
        registered_source_artifact_id="child:snapshot",
        registered_source_artifact_hash=file_sha256(snapshot),
        registered_source_artifact_path=str(snapshot),
    )

    result = evaluate_stage1_reuse(
        {
            "paper_info": {"canonical_paper_key": "10.1000/guard"},
            "ai_summary": summary,
            "stage1_reuse": {"binding": binding.to_dict()},
        },
        binding,
        registry=None,
    )

    assert result.reusable is False
    assert result.reason == "source_authority_artifact_id_missing"


def test_architecture_guard_summary_payload_is_bound_to_authority_bytes(tmp_path: Path) -> None:
    reuse_tree = _tree("services/stage1_reuse.py")
    verifier_calls = _function_call_names(reuse_tree, "_registered_source_is_verifiable")
    matcher = _definition(reuse_tree, "_authority_summary_matches")
    assert "_authority_summary_matches" in verifier_calls
    assert sum(_call_name(call).endswith("hash_json") for call in _calls(matcher)) >= 2

    authoritative = {"summary": "authority"}
    authority_path = tmp_path / "authority.json"
    _write_summary_authority(authority_path, authoritative)
    matching_binding = _binding(
        normalized_summary_payload_hash=hash_json(authoritative),
        summary_payload_hash=hash_json(authoritative),
    )

    imported_ok, imported_reason = _authority_summary_matches(
        path=str(authority_path),
        canonical_paper_key="10.1000/guard",
        previous_summary={"ai_summary": {"summary": "tampered import"}},
        binding=matching_binding,
    )
    bound_ok, bound_reason = _authority_summary_matches(
        path=str(authority_path),
        canonical_paper_key="10.1000/guard",
        previous_summary={"ai_summary": authoritative},
        binding=_binding(normalized_summary_payload_hash="9" * 64),
    )

    assert imported_ok is False
    assert imported_reason == "registered_source_artifact_payload_mismatch"
    assert bound_ok is False
    assert bound_reason == "registered_source_artifact_summary_payload_hash_mismatch"


def test_architecture_guard_provider_generated_reuse_requires_original_closure(
    tmp_path: Path,
) -> None:
    summary = {"summary": "provider result"}
    authority_path = tmp_path / "provider-summary.json"
    _write_summary_authority(authority_path, summary)
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "parent-job")
    record = registry.register_file(
        artifact_role="summary_source",
        artifact_type="summary_file",
        artifact_version="v1",
        path=authority_path,
        producer="architecture-guard",
        artifact_id="parent:summary",
    )
    binding = _binding(
        source_kind="stage1_provider_generated",
        normalized_summary_payload_hash=hash_json(summary),
        summary_payload_hash=hash_json(summary),
        source_authority_job_id="parent-job",
        source_authority_artifact_id=record.artifact_id,
        source_authority_artifact_hash=record.content_hash,
        source_authority_artifact_path=record.path,
        extra={"source_kind": "stage1_provider_generated", "provider_transport_count": 1},
    )

    result = evaluate_stage1_reuse(
        {
            "paper_info": {"canonical_paper_key": "10.1000/guard"},
            "ai_summary": summary,
            "provider": {"transport_count": 1},
            "stage1_reuse": {"binding": binding.to_dict()},
        },
        binding,
        registry=registry,
    )

    assert result.reusable is False
    assert result.reason == "source_provider_receipt_closure_missing"


def test_architecture_guard_omitted_validation_policy_remains_tri_state() -> None:
    boundaries = (
        (_tree("runtime/job_spec.py"), ("RuntimeJobSpec", "to_job_request")),
        (_tree("services/job_runner.py"), ("build_job_request_from_mapping",)),
    )
    for tree, qualified_name in boundaries:
        boundary = _definition(tree, *qualified_name)
        assert _bool_calls_touching_policy(boundary) == []
        assert _defaulted_policy_gets(boundary) == []

    spec = RuntimeJobSpec(
        project_name="guard",
        source=RuntimeSourceSpec(mode="direct", pdf_folder="C:/papers"),
        action="run_all",
    )
    direct_request = spec.to_job_request()
    mapped_request = build_job_request_from_mapping(
        {"project_name": "guard", "pdf_folder": "C:/papers"}
    )
    for request in (direct_request, mapped_request):
        assert request.validation_required is None
        assert request.require_clean_validation is None
        assert request.allow_unvalidated_when_validation_optional is None


def test_architecture_guard_gui_and_queue_delegate_validation_defaults() -> None:
    gui_tree = _tree("gui/app.py")
    gui_builder = _definition(gui_tree, "WorkspaceController", "_build_queue_job_spec")
    queue_tree = _tree("services/queue_service.py")
    queue_to_dict = _definition(queue_tree, "QueueJobSpec", "to_dict")
    queue_from_dict = _definition(queue_tree, "QueueJobSpec", "from_dict")
    queue_runner = _definition(queue_tree, "QueueRunner", "_process_job")

    for boundary in (gui_builder, queue_to_dict, queue_from_dict, queue_runner):
        calls = {_call_name(call) for call in _calls(boundary)}
        assert not any(name.endswith("build_stage_plan") for name in calls)
        assert not any(name.endswith("review_validation_enabled") for name in calls)
        assert _bool_calls_touching_policy(boundary) == []
        assert _defaulted_policy_gets(boundary) == []
    assert any(
        name.endswith("build_job_request_from_mapping")
        for name in {_call_name(call) for call in _calls(queue_runner)}
    )

    omitted = QueueJobSpec(job_id="job-1", job_type="run_all", project_name="guard")
    assert VALIDATION_POLICY_FIELDS.isdisjoint(omitted.to_dict()["parameters"])
    explicit_none = QueueJobSpec(
        job_id="job-2",
        job_type="run_all",
        project_name="guard",
        parameters={field: None for field in VALIDATION_POLICY_FIELDS},
    )
    restored = QueueJobSpec.from_dict(explicit_none.to_dict())
    assert all(restored.parameters[field] is None for field in VALIDATION_POLICY_FIELDS)


def test_architecture_guard_zero_call_policy_uses_production_artifact_types() -> None:
    review_tree = _tree("services/review_generation_service.py")
    review_writer = _definition(
        review_tree,
        "ReviewGenerationService",
        "_persist_review_replay",
    )
    review_types = _resolved_keyword_strings(review_tree, review_writer, "artifact_type")
    assert review_types == {"review_replay_ledger"}

    outline_policy = set(zero_call_evidence_policy("outline"))
    review_policy = set(zero_call_evidence_policy("review"))
    assert MODEL_REPLAY_ARTIFACT_TYPE in outline_policy
    assert review_types.issubset(review_policy)
    assert {
        "outline_call_plan",
        "outline_replay_evidence",
        "review_replay_evidence",
    }.isdisjoint(outline_policy | review_policy)


def test_architecture_guard_canonical_outcome_reader_uses_registry_not_projection() -> None:
    outcome_tree = _tree("services/job_outcome.py")
    reader = _definition(outcome_tree, "load_canonical_job_outcome")
    calls = {_call_name(call) for call in _calls(reader)}
    identifiers = _identifier_names(reader)
    literals = _string_literals(reader)

    assert "registry" in _argument_names(reader)
    assert any(name.endswith(".get") for name in calls)
    assert any(name.endswith("_verify_ready_artifact") for name in calls)
    assert any(name.endswith("JobOutcomeV1.from_dict") for name in calls)
    assert "job_outcome_v1.json" not in literals
    assert not any("compatibility" in name.lower() or "projection" in name.lower() for name in identifiers)


def test_architecture_guard_outcome_projection_writer_is_lease_fenced() -> None:
    outcome_tree = _tree("services/job_outcome.py")
    writer = _definition(outcome_tree, "publish_job_outcome_compatibility_projection")
    writer_calls = {_call_name(call) for call in _calls(writer)}
    assert "publication_context" in _argument_names(writer)
    assert "write_compatibility_json" in _string_literals(writer)
    assert "writer" in writer_calls
    assert not any(
        name.endswith(("atomic_write_json", "os.replace", "write_text", "write_bytes"))
        for name in writer_calls
    )

    call_sites: list[tuple[Path, ast.Call]] = []
    for root_name in ("runtime", "services", "validation", "gui"):
        for path in (REPO_ROOT / root_name).rglob("*.py"):
            if path == REPO_ROOT / "services/job_outcome.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            call_sites.extend(
                (path, call)
                for call in _calls(tree)
                if _call_name(call).endswith(
                    "publish_job_outcome_compatibility_projection"
                )
            )
    assert call_sites
    for path, call in call_sites:
        assert any(keyword.arg == "publication_context" for keyword in call.keywords), (
            f"projection publication omits its explicit execution context: "
            f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
        )

    queue_tree = _tree("services/queue_service.py")
    queue_writer = _definition(
        queue_tree,
        "QueuePublicationContext",
        "write_compatibility_json",
    )
    calls = _calls(queue_writer)
    call_names = {_call_name(call) for call in calls}
    assert any(name.endswith("_store_lock") for name in call_names)
    assert any(name.endswith("_assert_live_unlocked") for name in call_names)
    assert any(name.endswith(("_write_staged_bytes", "os.replace")) for name in call_names)

    guarded_blocks = [
        node
        for node in ast.walk(queue_writer)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and _call_name(item.context_expr).endswith("_store_lock")
            for item in node.items
        )
    ]
    assert any(
        any(
            _call_name(call).endswith("_assert_live_unlocked")
            for call in _calls(block)
        )
        and any(
            _call_name(call).endswith(("_write_staged_bytes", "os.replace"))
            for call in _calls(block)
        )
        for block in guarded_blocks
    )

    fence_line = min(
        call.lineno for call in calls if _call_name(call).endswith("_assert_live_unlocked")
    )
    commit_line = min(
        call.lineno
        for call in calls
        if _call_name(call).endswith(("_write_staged_bytes", "os.replace"))
    )
    assert fence_line < commit_line

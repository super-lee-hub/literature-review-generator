from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from outline.v3_artifacts import FinalOutline
from services.artifact_registry import (
    ArtifactClosureBatchError,
    ArtifactDependencyRefV2,
    ArtifactRegistry,
    UnverifiedDependency,
)
from tests.test_run_first_required_comparison_binding import (
    _build_case,
    _register_outline_artifact,
)
import validation.required_comparison_binding as required_binding


def test_resolver_closes_all_four_outline_roots_before_typed_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_case(tmp_path)
    events: list[tuple[str, Any]] = []
    four_root_results: list[Any] = []
    original_batch = ArtifactRegistry.verify_ready_artifact_closures
    original_read = required_binding._read_outline_payload

    def watched_batch(self, roots, **kwargs):
        roots = tuple(roots)
        root_ids = tuple(record.artifact_id for record in roots)
        events.append(("batch_start", root_ids))
        result = original_batch(self, roots, **kwargs)
        if len(root_ids) == 4:
            four_root_results.extend(result)
        events.append(("batch_done", root_ids))
        return result

    def watched_read(registry, record, **kwargs):
        events.append(("typed_read", record.artifact_id, record))
        return original_read(registry, record, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closures", watched_batch)
    monkeypatch.setattr(required_binding, "_read_outline_payload", watched_read)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert issues == []
    assert bindings
    batch_starts = [event for event in events if event[0] == "batch_start" and len(event[1]) == 4]
    batch_ends = [index for index, event in enumerate(events) if event[0] == "batch_done" and len(event[1]) == 4]
    typed_reads = [(index, event) for index, event in enumerate(events) if event[0] == "typed_read"]
    assert len(batch_starts) == 1
    assert batch_starts[0][1] == (
        fixture["outline_record"].artifact_id,
        fixture["adoption_record"].artifact_id,
        fixture["packet_record"].artifact_id,
        fixture["relation_record"].artifact_id,
    )
    assert len(batch_ends) == 1
    assert len(typed_reads) == 4
    assert all(index > batch_ends[0] for index, _event in typed_reads)
    returned_by_id = {record.artifact_id: record for record in four_root_results}
    assert len(returned_by_id) == 4
    assert all(event[2] is returned_by_id[event[1]] for _index, event in typed_reads)


@pytest.mark.parametrize(
    ("root_index", "prefix"),
    [
        (0, "required_comparison_outline_binding_untrusted:"),
        (1, "required_comparison_outline_binding_untrusted:"),
        (2, "required_comparison_scope_untrusted:"),
        (3, "required_comparison_scope_untrusted:"),
    ],
)
def test_resolver_maps_batch_root_failures_to_legacy_issue_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_index: int,
    prefix: str,
) -> None:
    fixture = _build_case(tmp_path)
    original_batch = ArtifactRegistry.verify_ready_artifact_closures
    typed_reads: list[str] = []
    original_read = required_binding._read_outline_payload

    def fail_four_roots(self, roots, **kwargs):
        roots = tuple(roots)
        if len(roots) == 1:
            return original_batch(self, roots, **kwargs)
        raise ArtifactClosureBatchError(
            root_index,
            UnverifiedDependency(f"synthetic root failure {root_index}"),
        )

    def counted_read(registry, record, **kwargs):
        typed_reads.append(record.artifact_id)
        return original_read(registry, record, **kwargs)

    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closures", fail_four_roots)
    monkeypatch.setattr(required_binding, "_read_outline_payload", counted_read)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert issues == [f"{prefix}synthetic root failure {root_index}"]
    assert typed_reads == []


def _register_historical_outline(fixture: dict[str, Any]):
    payload = {
        "title": "Previously adopted outline",
        "sections": [],
        "candidate_id": "candidate-historical",
        "paper_keys": ["paper-a", "paper-b"],
        "relation_ids": [],
        "source_hashes": {},
    }
    return _register_outline_artifact(
        fixture["workspace"],
        fixture["registry"],
        artifact_id="outline-v3:historical_final_outline",
        artifact_role="outline_v3_node_output",
        artifact_class=FinalOutline,
        payload=payload,
    )


def _replace_adoption_dependencies(
    registry: ArtifactRegistry,
    adoption_id: str,
    refs: list[ArtifactDependencyRefV2],
) -> None:
    registry_path = Path(registry.registry_path)
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    row = next(item for item in payload["artifacts"] if item["artifact_id"] == adoption_id)
    row["depends_on"] = [ref.to_dict() for ref in refs]
    registry_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    registry.reload()


def test_adoption_duplicate_matching_refs_and_extra_historical_edge_remain_accepted(
    tmp_path: Path,
) -> None:
    fixture = _build_case(tmp_path)
    historical = _register_historical_outline(fixture)
    named_ref = ArtifactDependencyRefV2.from_record(fixture["outline_record"])
    historical_ref = ArtifactDependencyRefV2.from_record(historical)
    _replace_adoption_dependencies(
        fixture["registry"],
        fixture["adoption_record"].artifact_id,
        [named_ref, named_ref, historical_ref],
    )

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert issues == []
    assert bindings


def test_adoption_without_any_exact_named_outline_ref_keeps_legacy_issue(
    tmp_path: Path,
) -> None:
    fixture = _build_case(tmp_path)
    historical = _register_historical_outline(fixture)
    historical_ref = ArtifactDependencyRefV2.from_record(historical)
    _replace_adoption_dependencies(
        fixture["registry"],
        fixture["adoption_record"].artifact_id,
        [historical_ref],
    )

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert issues == [
        "required_comparison_outline_binding_untrusted:"
        "adoption does not bind the named final Outline"
    ]


@pytest.mark.parametrize(
    ("prior_failure", "expected_issue"),
    [
        (
            "outline_closure",
            "required_comparison_outline_binding_untrusted:",
        ),
        (
            "typed_outline",
            "required_comparison_outline_binding_untrusted:injected typed outline failure",
        ),
        (
            "adoption_binding",
            "required_comparison_outline_binding_untrusted:adoption does not bind the named final Outline",
        ),
    ],
)
def test_missing_scope_candidate_preserves_earlier_outline_adoption_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_failure: str,
    expected_issue: str,
) -> None:
    fixture = _build_case(tmp_path)
    original_dependency = required_binding._verified_dependency
    original_batch = ArtifactRegistry.verify_ready_artifact_closures
    original_read = required_binding._read_outline_payload
    batch_sizes: list[int] = []

    def missing_packet(registry, owner, *, artifact_type):
        if artifact_type == "section_evidence_packet_set":
            raise ValueError("injected missing packet candidate")
        return original_dependency(registry, owner, artifact_type=artifact_type)

    def counted_batch(self, roots, **kwargs):
        roots = tuple(roots)
        batch_sizes.append(len(roots))
        return original_batch(self, roots, **kwargs)

    monkeypatch.setattr(required_binding, "_verified_dependency", missing_packet)
    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closures", counted_batch)

    if prior_failure == "outline_closure":
        Path(fixture["outline_record"].path).write_bytes(b"tampered outline bytes")
    elif prior_failure == "typed_outline":
        def fail_typed_outline(registry, record, *, artifact_class, **kwargs):
            if artifact_class is FinalOutline:
                raise ValueError("injected typed outline failure")
            return original_read(
                registry, record, artifact_class=artifact_class, **kwargs
            )

        monkeypatch.setattr(required_binding, "_read_outline_payload", fail_typed_outline)
    else:
        def mismatch_adoption(registry, record, *, artifact_class, **kwargs):
            payload = original_read(
                registry, record, artifact_class=artifact_class, **kwargs
            )
            if artifact_class.__name__ == "AdoptedOutline":
                return {**payload, "final_outline": {"mismatched": True}}
            return payload

        monkeypatch.setattr(required_binding, "_read_outline_payload", mismatch_adoption)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert len(issues) == 1
    assert issues[0].startswith(expected_issue)
    assert "injected missing packet candidate" not in issues[0]
    assert batch_sizes == [1, 2]


@pytest.mark.parametrize(
    ("missing_artifact_type", "expected_issue"),
    [
        (
            "section_evidence_packet_set",
            "required_comparison_scope_untrusted:injected missing section_evidence_packet_set candidate",
        ),
        (
            "confirmed_global_relation_map",
            "required_comparison_scope_untrusted:injected missing confirmed_global_relation_map candidate",
        ),
    ],
)
def test_missing_scope_candidate_returns_scope_issue_without_scope_payload_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_artifact_type: str,
    expected_issue: str,
) -> None:
    fixture = _build_case(tmp_path)
    original_dependency = required_binding._verified_dependency
    original_batch = ArtifactRegistry.verify_ready_artifact_closures
    original_read = required_binding._read_outline_payload
    batch_sizes: list[int] = []
    read_types: list[str] = []

    def missing_candidate(registry, owner, *, artifact_type):
        if artifact_type == missing_artifact_type:
            raise ValueError(f"injected missing {missing_artifact_type} candidate")
        return original_dependency(registry, owner, artifact_type=artifact_type)

    def counted_batch(self, roots, **kwargs):
        roots = tuple(roots)
        batch_sizes.append(len(roots))
        return original_batch(self, roots, **kwargs)

    def counted_read(registry, record, *, artifact_class, **kwargs):
        read_types.append(artifact_class.__name__)
        return original_read(registry, record, artifact_class=artifact_class, **kwargs)

    monkeypatch.setattr(required_binding, "_verified_dependency", missing_candidate)
    monkeypatch.setattr(ArtifactRegistry, "verify_ready_artifact_closures", counted_batch)
    monkeypatch.setattr(required_binding, "_read_outline_payload", counted_read)

    bindings, issues = required_binding.resolve_required_comparison_task_bindings(
        fixture["registry"], fixture["draft"],
    )

    assert bindings == {}
    assert issues == [expected_issue]
    assert batch_sizes == [1, 2]
    assert read_types == ["FinalOutline", "AdoptedOutline"]


def test_resolver_propagates_unexpected_runtime_error_from_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _build_case(tmp_path)

    def unexpected(*_args, **_kwargs):
        raise RuntimeError("unexpected resolver traversal programming error")

    monkeypatch.setattr(
        fixture["registry"], "_verify_ready_dependency_closure", unexpected
    )

    with pytest.raises(RuntimeError, match="unexpected resolver .* programming error") as caught:
        required_binding.resolve_required_comparison_task_bindings(
            fixture["registry"], fixture["draft"],
        )
    assert type(caught.value) is RuntimeError

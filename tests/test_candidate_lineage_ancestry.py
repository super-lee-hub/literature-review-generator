"""Candidate lineage comes only from the current roots' real verified ancestry."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
from typing import Any

import pytest

from outline.candidate_lineage_ancestry import load_verified_semantic_lineage_claims
from outline.v3_artifacts import OutlineArtifact
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    UnverifiedDependency,
)
from services.job_workspace import publish_json_artifact
from services.queue_service import LocalPublicationContext


_EXPANDED_ROLE = "outline_v3_semantic_identifier_expanded_output"


@pytest.fixture(autouse=True)
def _no_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("candidate ancestry units must not use network or providers")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _publish(
    registry: ArtifactRegistry,
    artifact_id: str,
    payload: dict[str, Any],
    *,
    role: str = "outline_v3_node_output",
    dependencies: tuple[ArtifactDependencyRefV2, ...] = (),
    resolver: Any = None,
) -> ArtifactRecord:
    artifact = OutlineArtifact(
        job_id=registry.job_id,
        dependency_hashes={row.artifact_id: row.content_hash for row in dependencies},
        payload=payload,
    )
    path = Path(registry.registry_path).parent / f"{artifact_id.replace(':', '_')}.json"
    return publish_json_artifact(
        LocalPublicationContext(), registry, path, artifact.to_dict(),
        artifact_id=artifact_id,
        artifact_role=role,
        artifact_type="outline_artifact",
        artifact_version="v3",
        producer="outline.v3_executor.OutlineV3Executor",
        depends_on=dependencies,
        external_registry_resolver=resolver,
        metadata={"job_id": registry.job_id, "content_hash": artifact.content_hash},
    )


def _claim(claim_id: str, source_id: str) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "paper_keys": ["paper:canonical"],
        "text": "A source-bounded comparison retains its recorded qualifier.",
        "source_claim_ids": [source_id],
        "evidence_ids": ["evidence:canonical"],
        "source_field_ids": ["field:qualifier"],
    }


@pytest.fixture
def ancestry_case(tmp_path: Path) -> dict[str, Any]:
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "candidate-lineage-job")
    leaf_claim = _claim("synthesis:cross_group_comparison:leaf", "claim:canonical")
    leaf = _publish(
        registry, "outline-v3:cross_group_comparison",
        {"provider_output": {"bridge_claims": [leaf_claim]}},
    )
    middle_claim = _claim("synthesis:global_synthesis:reduce:1:1", leaf_claim["claim_id"])
    middle = _publish(
        registry, "semantic-identifier-output:retained-reduce-1-1",
        {"synthesis_claims": [middle_claim]}, role=_EXPANDED_ROLE,
        dependencies=(ArtifactDependencyRefV2.from_record(leaf),),
    )
    identifier_map = _publish(
        registry, "semantic-identifier-map:final", {"test_marker": "actual-map-edge"},
        role="outline_v3_semantic_identifier_alias_map",
        dependencies=(ArtifactDependencyRefV2.from_record(middle),),
    )
    final_claim = _claim("synthesis:global_synthesis:final", middle_claim["claim_id"])
    root = _publish(
        registry, "outline-v3:global_synthesis",
        {"provider_output": {"synthesis_claims": [final_claim]}},
        dependencies=(ArtifactDependencyRefV2.from_record(identifier_map),),
    )
    return {
        "registry": registry, "leaf": leaf, "middle": middle, "map": identifier_map,
        "root": root, "claims": (leaf_claim, middle_claim, final_claim),
    }


def _rewrite_registry(registry: ArtifactRegistry, mutate: Any) -> None:
    path = Path(registry.registry_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    # Deliberately retain the revision: a full record-map pin must detect this.
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _mutate_row(registry: ArtifactRegistry, artifact_id: str, **changes: Any) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        row = next(row for row in payload["artifacts"] if row["artifact_id"] == artifact_id)
        row.update(changes)

    _rewrite_registry(registry, mutate)


def test_real_nested_expanded_ancestor_is_included_through_identifier_map(ancestry_case, monkeypatch):
    case = ancestry_case
    registry = case["registry"]
    original = registry.verify_ready_artifact_closures
    calls = []

    def verified(roots, **kwargs):
        roots = tuple(roots)
        revision, record_map = kwargs["expected_record_snapshot"]
        assert revision == registry.revision
        assert record_map[case["middle"].artifact_id] == case["middle"]
        calls.append(roots)
        return original(roots, **kwargs)

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", verified)
    claims = load_verified_semantic_lineage_claims(registry, [case["root"]])

    assert {row["claim_id"] for row in claims} == {row["claim_id"] for row in case["claims"]}
    assert {row["claim_id"]: row for row in claims} == {
        row["claim_id"]: row for row in case["claims"]
    }
    assert calls == [(case["root"],)]


@pytest.mark.parametrize(("artifact_id", "field"), [
    ("outline-v3:topic_synthesis", "claims"),
    ("outline-v3:cross_group_comparison", "bridge_claims"),
    ("outline-v3:global_synthesis", "synthesis_claims"),
])
@pytest.mark.parametrize("wrapped", [False, True])
def test_existing_aggregate_ids_extract_exact_direct_or_provider_payload_claims(
    tmp_path, artifact_id, field, wrapped,
):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "aggregate-lineage-job")
    claim = _claim("synthesis:global_synthesis:aggregate", "claim:canonical")
    output = {field: [claim]}
    root = _publish(registry, artifact_id, {"provider_output": output} if wrapped else output)

    assert load_verified_semantic_lineage_claims(registry, [root]) == (claim,)


def test_unrelated_same_role_corrupt_artifact_is_not_scanned_or_parsed(ancestry_case):
    case = ancestry_case
    unrelated = _publish(
        case["registry"], "semantic-identifier-output:unrelated",
        {"synthesis_claims": [_claim("synthesis:global_synthesis:unrelated", "claim:foreign")]},
        role=_EXPANDED_ROLE,
    )
    Path(unrelated.path).write_bytes(b"corrupt unrelated bytes")
    claims = load_verified_semantic_lineage_claims(case["registry"], [case["root"]])

    assert {row["claim_id"] for row in claims} == {row["claim_id"] for row in case["claims"]}


@pytest.mark.parametrize("root_fault", ["owner", "status", "metadata"])
def test_stale_foreign_or_nonready_root_rejects(ancestry_case, root_fault):
    case = ancestry_case
    changes = {
        "owner": {"job_id": "foreign-job"},
        "status": {"status": "dirty"},
        "metadata": {"metadata": {**case["root"].metadata, "late": "changed"}},
    }[root_fault]
    with pytest.raises(UnverifiedDependency, match="root is stale, foreign or not READY"):
        load_verified_semantic_lineage_claims(
            case["registry"], [replace(case["root"], **changes)],
        )


@pytest.mark.parametrize("fault", ["missing", "dirty", "owner", "dependency_owner"])
def test_actual_ancestor_missing_dirty_or_foreign_identity_rejects(ancestry_case, fault):
    case = ancestry_case
    registry = case["registry"]
    middle = case["middle"]
    if fault == "missing":
        _rewrite_registry(registry, lambda payload: payload.update(artifacts=[
            row for row in payload["artifacts"] if row["artifact_id"] != middle.artifact_id
        ]))
    elif fault == "dirty":
        _mutate_row(registry, middle.artifact_id, status="dirty")
    elif fault == "owner":
        _mutate_row(registry, middle.artifact_id, job_id="foreign-job")
    else:
        wrong = replace(case["map"].depends_on[0], job_id="foreign-job")
        _mutate_row(registry, case["map"].artifact_id, depends_on=[wrong.to_dict()])

    with pytest.raises(RegistryError):
        load_verified_semantic_lineage_claims(registry, [case["root"]])


def test_actual_ancestor_corrupt_bytes_reject_before_claims_escape(ancestry_case):
    case = ancestry_case
    Path(case["middle"].path).write_bytes(b"changed ancestor")
    with pytest.raises(RegistryError, match="content hash changed"):
        load_verified_semantic_lineage_claims(case["registry"], [case["root"]])


@pytest.mark.parametrize("phase", ["before_union", "after_union"])
@pytest.mark.parametrize("fault", ["bytes", "metadata"])
def test_late_bytes_or_same_revision_metadata_change_cannot_escape_pin(
    ancestry_case, monkeypatch, phase, fault,
):
    case = ancestry_case
    registry = case["registry"]
    original = registry.verify_ready_artifact_closures
    calls = 0

    def mutate() -> None:
        if fault == "bytes":
            path = Path(case["middle"].path)
            path.write_bytes(path.read_bytes() + b" ")
        else:
            _mutate_row(
                registry, case["middle"].artifact_id,
                metadata={**case["middle"].metadata, "late": "changed"},
            )

    def verified(roots, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "before_union":
            mutate()
        result = original(roots, **kwargs)
        if phase == "after_union":
            mutate()
        return result

    monkeypatch.setattr(registry, "verify_ready_artifact_closures", verified)
    with pytest.raises(RegistryError):
        load_verified_semantic_lineage_claims(registry, [case["root"]])
    assert calls == 1


@pytest.mark.parametrize("field", ["claims", "bridge_claims", "synthesis_claims"])
@pytest.mark.parametrize("rows", [None, {}, "foreign", ["not-a-mapping"]])
def test_malformed_claim_array_rejects_despite_valid_registered_envelope(tmp_path, field, rows):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "malformed-lineage-job")
    root = _publish(
        registry, "semantic-identifier-output:malformed", {field: rows}, role=_EXPANDED_ROLE,
    )
    with pytest.raises(UnverifiedDependency, match="not a claim array"):
        load_verified_semantic_lineage_claims(registry, [root])


@pytest.mark.parametrize("payload", ["not-an-output", None, []])
def test_malformed_provider_output_is_not_treated_as_empty_lineage(tmp_path, payload):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "bad-provider-output-job")
    root = _publish(registry, "outline-v3:global_synthesis", {"provider_output": payload})
    with pytest.raises(UnverifiedDependency, match="provider output is invalid"):
        load_verified_semantic_lineage_claims(registry, [root])


def test_physical_alias_response_ancestor_is_verified_but_not_a_canonical_definition(ancestry_case):
    case = ancestry_case
    physical = _publish(
        case["registry"], "semantic-provider-output:physical-alias",
        {"synthesis_claims": [_claim("synthesis:global_synthesis:physical", "alias:p001")]},
        role="outline_v3_semantic_provider_output",
    )
    root = _publish(
        case["registry"], "outline-v3:global_synthesis",
        {"provider_output": {"synthesis_claims": [case["claims"][-1]]}},
        dependencies=tuple(ArtifactDependencyRefV2.from_record(row) for row in (case["map"], physical)),
    )
    claims = load_verified_semantic_lineage_claims(case["registry"], [root])

    assert {row["claim_id"] for row in claims} == {row["claim_id"] for row in case["claims"]}
    Path(physical.path).write_bytes(b"changed physical receipt response")
    with pytest.raises(RegistryError):
        load_verified_semantic_lineage_claims(case["registry"], [root])


@pytest.mark.parametrize("fault", ["no_resolver", "foreign_bytes", "authorized"])
def test_external_dependency_needs_real_closure_authority_and_never_contributes_claims(tmp_path, fault):
    remote = ArtifactRegistry(tmp_path / "remote" / "artifact_registry.json", "remote-lineage-job")
    remote_claim = _claim("synthesis:global_synthesis:foreign", "claim:remote")
    foreign = _publish(remote, "semantic-identifier-output:foreign", {"claims": [remote_claim]}, role=_EXPANDED_ROLE)
    resolver = lambda job_id: remote if job_id == remote.job_id else None
    local = ArtifactRegistry(tmp_path / "local" / "artifact_registry.json", "local-lineage-job")
    local_claim = _claim("synthesis:global_synthesis:local", "claim:local")
    root = _publish(
        local, "outline-v3:global_synthesis", {"synthesis_claims": [local_claim]},
        dependencies=(ArtifactDependencyRefV2.from_record(foreign, dependency_kind="external_job"),),
        resolver=resolver,
    )
    if fault == "authorized":
        assert load_verified_semantic_lineage_claims(
            local, [root], external_registry_resolver=resolver,
        ) == (local_claim,)
    else:
        if fault == "foreign_bytes":
            Path(foreign.path).write_bytes(b"changed external ancestor")
        with pytest.raises(RegistryError):
            load_verified_semantic_lineage_claims(
                local, [root],
                external_registry_resolver=None if fault == "no_resolver" else resolver,
            )


def test_empty_roots_do_not_search_the_registry(tmp_path, monkeypatch):
    registry = ArtifactRegistry(tmp_path / "artifact_registry.json", "empty-lineage-job")

    def denied():
        pytest.fail("empty roots must not inspect unrelated records")

    monkeypatch.setattr(registry, "capture_record_snapshot", denied)
    assert load_verified_semantic_lineage_claims(registry, []) == ()

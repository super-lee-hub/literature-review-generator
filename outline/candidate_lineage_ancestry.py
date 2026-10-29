"""Read lineage definitions only from the current roots' verified ancestors."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import hashlib
import json
from pathlib import Path
from typing import Any

from services.artifact_registry import (
    ArtifactDependencyRefV2, ArtifactRecord, ArtifactRegistry, RegistrySnapshotChanged,
    UnverifiedDependency,
)

_AGGREGATE_IDS = frozenset({
    "outline-v3:topic_synthesis", "outline-v3:cross_group_comparison", "outline-v3:global_synthesis",
})
_EXPANDED_ROLE = "outline_v3_semantic_identifier_expanded_output"


def load_verified_semantic_lineage_claims(
    registry: ArtifactRegistry,
    roots: Iterable[ArtifactRecord],
    *,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> tuple[Mapping[str, Any], ...]:
    """Return intermediate definitions, never additional candidate output groups.

    Parsing is provisional until one pinned, recursive union proof succeeds.
    We follow only actual local dependency edges; physical alias responses and
    unrelated READY records are not interpreted as canonical claim definitions.
    """
    root_values = tuple(roots)
    if not root_values:
        return ()
    revision, records = registry.capture_record_snapshot()
    by_id = {record.artifact_id: record for record in records}
    for root in root_values:
        if (root.job_id != registry.job_id or root.status != "ready"
                or by_id.get(root.artifact_id) != root):
            raise UnverifiedDependency("candidate lineage root is stale, foreign or not READY")
    pending = list(root_values)
    seen = set()
    selected: list[ArtifactRecord] = []
    claims: list[Mapping[str, Any]] = []
    while pending:
        record = pending.pop()
        if record.artifact_id in seen:
            continue
        seen.add(record.artifact_id)
        if record.job_id != registry.job_id or record.status != "ready":
            raise UnverifiedDependency("candidate lineage ancestor is foreign or not READY")
        for dependency in record.depends_on:
            if dependency.dependency_kind != "local_job":
                # External dependencies still participate in the final full
                # proof, but their payload is never treated as local synthesis.
                continue
            target = by_id.get(dependency.artifact_id)
            if target is None or ArtifactDependencyRefV2.from_record(target) != dependency:
                raise UnverifiedDependency("candidate lineage dependency identity changed")
            pending.append(target)
        if record.artifact_type != "outline_artifact" or record.artifact_version != "v3":
            continue
        if not (record.artifact_role == _EXPANDED_ROLE
                or record.artifact_id in _AGGREGATE_IDS):
            continue
        registry._verify_ready_artifact(record)
        raw = Path(record.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.content_hash:
            raise UnverifiedDependency("candidate lineage ancestor bytes changed before parsing")
        envelope = json.loads(raw)
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping) or envelope.get("job_id") != registry.job_id:
            raise UnverifiedDependency("candidate lineage ancestor envelope is invalid")
        output = payload.get("provider_output", payload)
        if not isinstance(output, Mapping):
            raise UnverifiedDependency("candidate lineage provider output is invalid")
        for key in ("claims", "bridge_claims", "synthesis_claims"):
            if key not in output:
                continue
            rows = output[key]
            if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
                raise UnverifiedDependency(f"candidate lineage {key} is not a claim array")
            claims.extend(dict(row) for row in rows)
        selected.append(record)
    # Includes cycle, full local/foreign identity/schema, physical/expanded
    # identifier maps and all final byte/Registry fences. No provisional claim
    # leaves this function if any part of that actual ancestry fails.
    registry.verify_ready_artifact_closures(
        root_values, external_registry_resolver=external_registry_resolver,
        expected_record_snapshot=(revision, by_id),
    )
    for record in selected:
        if hashlib.sha256(Path(record.path).read_bytes()).hexdigest() != record.content_hash:
            raise UnverifiedDependency("candidate lineage ancestor bytes changed after parsing")
    final_revision, final_records = registry.capture_record_snapshot()
    if (revision != final_revision
            or by_id != {record.artifact_id: record for record in final_records}):
        raise RegistrySnapshotChanged("candidate lineage Registry snapshot changed during verification")
    return tuple(claims)

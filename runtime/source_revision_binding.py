"""Bind a freshly admitted SourceRevision plan to the executing child job."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from runtime.provider_runtime import hash_json
from runtime.source_revision_replay import SourceRevisionReplayContext
from services.artifact_registry import ArtifactDependencyRefV2, ArtifactRegistry
from services.job_workspace import JobWorkspace, publish_json_artifact


def open_registered_source_revision_replay_context(
    registry: ArtifactRegistry,
    resolver: Callable[[str], ArtifactRegistry | None],
) -> SourceRevisionReplayContext:
    """Reopen strict authority for status/export without publishing anything."""
    record = registry.get("source_revision_plan")
    if record is None:
        raise RuntimeError("SourceRevision closure has no child plan")
    registry._verify_ready_artifact(record)
    body = json.loads(Path(record.path).read_bytes())
    parent = resolver(str(body["parent"]["job_id"]))
    if parent is None or Path(parent.registry_path).resolve() != Path(body["parent"]["registry_path"]).resolve():
        raise RuntimeError("SourceRevision closure cannot resolve its exact parent")
    return SourceRevisionReplayContext(
        child_registry=registry, plan_record=record, plan_body=body,
        parent_registry=parent, external_registry_resolver=resolver,
    )


def verify_registered_source_revision_reuse(
    context: SourceRevisionReplayContext,
    expected_call: Mapping[str, Any],
) -> None:
    """Rebuild current scope from registered child bytes, never from the proof."""
    from runtime.source_revision_replay import build_writer_source_revision_scope_v1

    registry = context.child_registry
    proof = registry.get(str(expected_call.get("reuse_evidence_artifact_id") or ""))
    if proof is None:
        raise RuntimeError("SourceRevision closure proof is missing")
    registry._verify_ready_artifact(proof)
    payload = json.loads(Path(proof.path).read_bytes())
    output = registry.get(str(payload["child_output_ref"]["artifact_id"]))
    if output is None:
        raise RuntimeError("SourceRevision closure output is missing")
    registry._verify_ready_artifact(output)
    route = {key: expected_call[key] for key in (
        "provider", "model", "endpoint", "endpoint_type", "config_hash", "schema_hash",
    )}
    if expected_call.get("stage_name") == "stage3_review" and output.artifact_type == "review_section":
        document = json.loads(Path(output.path).read_bytes())
        if document.get("section_id") != expected_call.get("node_id"):
            raise RuntimeError("SourceRevision closure section identity differs")
        scope = build_writer_source_revision_scope_v1(
            document["section"]["writer_task_scope"], document["binding"],
        )
        route["route"] = "Writer_API"
    elif expected_call.get("stage_name") == "outline_v3":
        bindings = {}
        for audit in registry.list_records():
            if (audit.status != "ready" or audit.artifact_type != "outline_request_payload_audit"
                    or not audit.artifact_id.startswith(f"outline-v3:request_payload_audit:{expected_call['closure_epoch_id']}")):
                continue
            registry.verify_ready_artifact_closure(audit, external_registry_resolver=context.external_registry_resolver)
            for line in Path(audit.path).read_bytes().splitlines():
                row = json.loads(line)
                if ((row.get("call_id") or row.get("attempt_id")) != expected_call["call_id"]
                        or row.get("node_id") != str(expected_call["call_id"]).removeprefix("outline:")
                        or row.get("semantic_node_id") != str(expected_call["call_id"]).removeprefix("outline:")
                        or row.get("payload_hash") != expected_call["input_hash"]
                        or row.get("closure_epoch_id") != expected_call["closure_epoch_id"]
                        or row.get("semantic_validation_status") != "accepted"
                        or row.get("source_revision_lookup_status") != "match"
                        or row.get("provider_invoked") is not False):
                    continue
                current_route = {**route, "route": row["route"]["config_section"]}
                if row.get("route_fingerprint"):
                    current_route["route_fingerprint"] = row["route_fingerprint"]
                binding = (row["source_revision_scope"], current_route)
                bindings[hash_json(binding)] = binding
        if len(bindings) != 1:
            raise RuntimeError("SourceRevision closure has no unique accepted current Outline request audit")
        scope, route = next(iter(bindings.values()))
    else:
        raise RuntimeError("SourceRevision closure stage is not supported by this verifier")
    context.verify_child_reuse_proof(
        proof, child_expected_call=expected_call, child_output_record=output,
        current_scope=scope, route_identity=route,
        external_registry_resolver=context.external_registry_resolver,
    )


def source_revision_registry_paths(registry: ArtifactRegistry) -> tuple[str, ...]:
    """Discover only the declared parent path from a pinned child plan."""
    record = registry.get("source_revision_plan")
    if record is None:
        return ()
    from services.artifact_registry import file_sha256

    if (record.status != "ready" or record.artifact_type != "source_revision_plan"
            or file_sha256(record.path) != record.content_hash):
        raise RuntimeError("SourceRevision parent discovery has an invalid plan record")
    body = json.loads(Path(record.path).read_text(encoding="utf-8"))
    if body["child_job_id"] != registry.job_id:
        raise RuntimeError("SourceRevision parent discovery has a foreign child plan")
    return (str(body["parent"]["registry_path"]),)


def bind_source_revision_replay(
    *,
    plan_body: Mapping[str, Any],
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    publication_context: Any,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
) -> SourceRevisionReplayContext:
    """Publish exact child-owned authority, then invoke the strict replay binder.

    The context independently rebuilds admission from the persisted child spec.
    A caller-provided plan body is therefore never sufficient reuse authority.
    """
    parent = plan_body["parent"]
    parent_id = str(parent["job_id"])
    parent_path = Path(parent["registry_path"]).resolve(strict=True)
    parent_registry = ArtifactRegistry(parent_path, parent_id)

    def resolve(job_id: str) -> ArtifactRegistry | None:
        if job_id == parent_id:
            return parent_registry
        return external_registry_resolver(job_id)

    runtime_spec = registry.get("runtime_job_spec")
    if runtime_spec is None or runtime_spec.status != "ready":
        raise RuntimeError("SourceRevision execution requires the persisted child runtime spec")
    dependencies = [ArtifactDependencyRefV2.from_record(runtime_spec)]
    for ref in (*parent["authority_refs"], *parent["response_snapshot_refs"]):
        record = parent_registry.get(str(ref["artifact_id"]))
        if record is None or record.status != "ready" or any(
            str(getattr(record, field)) != str(ref[field])
            for field in ("job_id", "artifact_type", "path", "content_hash")
        ):
            raise RuntimeError("SourceRevision parent record changed before child binding")
        dependencies.append(ArtifactDependencyRefV2(
            dependency_kind="external_job", job_id=record.job_id,
            artifact_id=record.artifact_id, artifact_type=record.artifact_type,
            path=record.path, content_hash=record.content_hash,
        ))
    expected_dependencies = tuple(dict.fromkeys(dependencies))
    record = registry.get("source_revision_plan")
    if record is None:
        record = publish_json_artifact(
            publication_context, registry,
            workspace.artifact_path("source_revision_plan_v1.json"), dict(plan_body),
            artifact_id="source_revision_plan", artifact_role="source_revision_plan",
            artifact_type="source_revision_plan", artifact_version="v1",
            producer="runtime.source_revision_binding.bind_source_revision_replay",
            depends_on=list(expected_dependencies), external_registry_resolver=resolve,
            metadata={"job_id": workspace.job_id, "plan_content_hash": hash_json(plan_body)},
        )
    elif (
        record.status != "ready" or record.artifact_type != "source_revision_plan"
        or record.artifact_version != "v1" or tuple(record.depends_on) != expected_dependencies
        or json.loads(Path(record.path).read_text(encoding="utf-8")) != dict(plan_body)
    ):
        raise RuntimeError("persisted SourceRevision execution plan differs on resume")
    return SourceRevisionReplayContext(
        child_registry=registry, plan_record=record, plan_body=plan_body,
        parent_registry=parent_registry, external_registry_resolver=resolve,
    )

from __future__ import annotations

import json
from typing import Any

from outline.semantic_chunking import build_paper_content_layers
from outline.v3_artifacts import OutlineArtifact
from outline.v3_evidence import build_outline_evidence_views
from outline.v3_models import compute_v3_hash
from runtime.provider_runtime import hash_json
from services.artifact_registry import ArtifactDependencyRefV2
from services.job_workspace import publish_json_artifact
from services.queue_service import LocalPublicationContext


def bind_production_writer_sources(service, packets: list[dict[str, Any]]) -> None:
    registry, workspace = service.registry, service.workspace
    publication = LocalPublicationContext()
    summary_set_hash = hash_json(service.summaries)
    source_payload = {
        "artifact_type": "stage1_canonical_summaries", "artifact_version": "v1",
        "job_id": registry.job_id, "summary_set_hash": summary_set_hash,
        "summaries": service.summaries,
    }
    immutable_id = f"outline-v3:stage1-summaries:{summary_set_hash}"
    source = publish_json_artifact(
        publication, registry, workspace.artifact_path("writer_source_fixture/immutable.json"), source_payload,
        artifact_id=immutable_id, artifact_role="stage1_input", artifact_type="stage1_canonical_summaries",
        artifact_version="v1", producer="outline.v3_executor.OutlineV3Executor",
        metadata={"immutable": True, "summary_set_hash": summary_set_hash, "versioned_artifact_id": immutable_id},
    )
    pointer = publish_json_artifact(
        publication, registry, workspace.artifact_path("writer_source_fixture/current.json"), source_payload,
        artifact_id="stage1_summaries", artifact_role="stage1_input", artifact_type="stage1_canonical_summaries",
        artifact_version="v1", producer="outline.v3_executor.OutlineV3Executor",
        depends_on=[ArtifactDependencyRefV2.from_record(source)],
        metadata={"pointer_role": "current", "current_version_artifact_id": immutable_id, "summary_set_hash": summary_set_hash},
    )
    views = build_outline_evidence_views(service.summaries, registry.job_id)
    layers = build_paper_content_layers(service.summaries, views, job_id=registry.job_id)

    def publish(node_id, payload, input_hashes, dependencies):
        artifact = OutlineArtifact(
            job_id=registry.job_id, payload=payload, dependency_hashes=input_hashes,
        )
        return publish_json_artifact(
            publication, registry, workspace.artifact_path(f"writer_source_fixture/{node_id}.json"), artifact.to_dict(),
            artifact_id=f"outline-v3:{node_id}", artifact_role="outline_v3_node_output",
            artifact_type="outline_artifact", artifact_version="v3",
            producer="outline.v3_executor.OutlineV3Executor", depends_on=dependencies,
            metadata={"job_id": registry.job_id, "node_id": node_id, "content_hash": artifact.content_hash},
        ), artifact

    views_record, _views_artifact = publish(
        "outline_evidence_views", views.to_dict(), {"stage1_summaries": pointer.content_hash},
        [ArtifactDependencyRefV2.from_record(pointer)],
    )
    publish("outline_content_layers", layers.to_dict(), {"outline_evidence_views": compute_v3_hash(views.to_dict())},
            [ArtifactDependencyRefV2.from_record(views_record)])
    dossier = layers.dossiers[0]
    claims = [claim for claim in dossier.claims if claim.evidence_ids and claim.claim_type == "empirical_finding"]
    if not claims:
        claims = [claim for claim in dossier.claims if claim.evidence_ids]
    primary = claims[0]
    source_claim_ids = {primary.claim_id}
    evidence_ids = set(primary.evidence_ids)
    field_ids = {
        item.source_field_id for item in dossier.source_field_ledger
        if item.source_value == primary.text or item.source_path == primary.source_locator
    }
    from services.writer_source_inventory import load_writer_source_inventory_v1

    inventory = load_writer_source_inventory_v1(registry)
    dependencies = inventory.paper_by_key()[dossier.paper_id].interpretation_dependencies
    pending = True
    while pending:
        previous_count = (len(source_claim_ids), len(evidence_ids), len(field_ids))
        for dependency in dependencies:
            if dependency.primary_claim_id in source_claim_ids:
                source_claim_ids.update(dependency.required_source_claim_ids)
                evidence_ids.update(dependency.required_evidence_ids)
                field_ids.update(dependency.required_source_field_ids)
        for claim in dossier.claims:
            if claim.claim_id in source_claim_ids:
                evidence_ids.update(claim.evidence_ids)
        pending = previous_count != (len(source_claim_ids), len(evidence_ids), len(field_ids))
    for packet in packets:
        packet["planned_claims"] = [primary.text]
        packet["claim_support"] = [{
            "claim": primary.text, "claim_id": "fixture:planned-claim",
            "paper_key": dossier.paper_id, "primary_claim_id": primary.claim_id,
            "source_claim_ids": sorted(source_claim_ids),
            "evidence_ids": sorted(evidence_ids), "source_field_ids": sorted(field_ids),
            "qualifier_source_claim_ids": sorted({
                identifier for item in dependencies if item.primary_claim_id in source_claim_ids
                for identifier in item.required_source_claim_ids
            }),
            "qualifier_evidence_ids": sorted({
                identifier for item in dependencies if item.primary_claim_id in source_claim_ids
                for identifier in item.required_evidence_ids
            }),
            "qualifier_source_field_ids": sorted({
                identifier for item in dependencies if item.primary_claim_id in source_claim_ids
                for identifier in item.required_source_field_ids
            }),
        }]
        packet["source_summary_hashes"] = [dossier.source_summary_hash]
        packet["evidence_view_hashes"] = [views.views[0].view_hash]
        packet["evidence_items"] = [{
            "paper_key": dossier.paper_id, "summary_hash": dossier.source_summary_hash,
            "view_hash": views.views[0].view_hash, "fields": {"findings": [primary.text]},
            "source_fields": views.views[0].source_fields, "interpretation_context": [],
        }]


def scoped_writer_content(prompt: str, text: str) -> dict[str, Any]:
    scope = json.loads(prompt)["writer_task_scope"]
    tasks = scope["tasks"]
    return {
        "blocks": [{
            "writer_task_id": task["writer_task_id"],
            "writer_output_unit_id": next(unit["writer_output_unit_id"] for unit in task["output_units"] if unit["required"]),
            "writer_task_basis_hash": scope["writer_task_basis_hash"], "text": text,
        } for task in tasks],
        "task_dispositions": [{
            "writer_task_id": task["writer_task_id"], "writer_task_basis_hash": scope["writer_task_basis_hash"],
            "disposition": "covered",
        } for task in tasks],
    }

from __future__ import annotations

from collections import Counter
import os
from typing import Callable, Mapping

from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
)
from validation.run_result import ValidationInputArtifactsV1


ExternalRegistryResolver = Callable[[str], ArtifactRegistry | None]
ValidationInputIdentity = tuple[str, str, str]


class ValidationInputDependencyError(ValueError):
    """Raised when declared Validation inputs cannot form one exact Registry closure."""


def declared_validation_input_identities(
    inputs: ValidationInputArtifactsV1,
) -> tuple[ValidationInputIdentity, ...]:
    inputs.validate()
    identities: list[ValidationInputIdentity] = []
    if inputs.review_draft_id:
        identities.append(
            ("review_draft", inputs.review_draft_id, inputs.review_draft_hash)
        )
    if inputs.citation_manifest_id:
        identities.append(
            (
                "citation_manifest",
                inputs.citation_manifest_id,
                inputs.citation_manifest_hash,
            )
        )
    if inputs.validation_source_binding_id:
        identities.append(
            (
                "validation_source_binding",
                inputs.validation_source_binding_id,
                inputs.validation_source_binding_hash,
            )
        )
    identities.extend(
        ("evidence_manifest", artifact_id, content_hash)
        for artifact_id, content_hash in zip(
            inputs.evidence_manifest_ids,
            inputs.evidence_manifest_hashes,
        )
    )
    return tuple(identities)


def _dependency_from_record(record: ArtifactRecord) -> ArtifactDependencyRefV2:
    return ArtifactDependencyRefV2(
        dependency_kind="local_job",
        job_id=record.job_id,
        artifact_id=record.artifact_id,
        artifact_type=record.artifact_type,
        path=record.path,
        content_hash=record.content_hash,
    )


def _dependency_key(ref: ArtifactDependencyRefV2) -> tuple[str, str, str, str, str, str]:
    normalized_path = (
        os.path.normcase(os.path.abspath(os.fspath(ref.path))) if ref.path else ""
    )
    return (
        ref.dependency_kind,
        ref.job_id,
        ref.artifact_id,
        ref.artifact_type,
        normalized_path,
        ref.content_hash,
    )


def _resolve_identity_candidates(
    registry: ArtifactRegistry,
    records: tuple[ArtifactRecord, ...],
    identity: ValidationInputIdentity,
) -> list[ArtifactDependencyRefV2]:
    artifact_type, artifact_id, content_hash = identity
    candidates: dict[
        tuple[str, str, str, str, str, str], ArtifactDependencyRefV2
    ] = {}
    local_record = registry.get(artifact_id)
    if (
        local_record is not None
        and local_record.artifact_type == artifact_type
        and local_record.content_hash == content_hash
    ):
        local_ref = _dependency_from_record(local_record)
        candidates[_dependency_key(local_ref)] = local_ref

    if artifact_type == "evidence_manifest":
        for record in records:
            if record.status != "ready":
                continue
            for dependency in record.depends_on:
                if (
                    dependency.artifact_id == artifact_id
                    and dependency.artifact_type == artifact_type
                    and dependency.content_hash == content_hash
                ):
                    candidates[_dependency_key(dependency)] = dependency
    return list(candidates.values())


def resolve_validation_input_dependencies(
    registry: ArtifactRegistry,
    inputs: ValidationInputArtifactsV1,
    *,
    external_registry_resolver: ExternalRegistryResolver | None = None,
) -> list[ArtifactDependencyRefV2]:
    """Resolve every declared input to one ready local or external Registry edge."""

    identities = declared_validation_input_identities(inputs)
    registry.reload()
    records = tuple(registry.list_records())
    dependencies: list[ArtifactDependencyRefV2] = []
    for identity in identities:
        artifact_type, artifact_id, _content_hash = identity
        candidates = _resolve_identity_candidates(registry, records, identity)
        if not candidates:
            raise ValidationInputDependencyError(
                "Validation input dependency is not registered with the declared identity: "
                f"{artifact_type}/{artifact_id}"
            )
        if len(candidates) != 1:
            raise ValidationInputDependencyError(
                "Validation input dependency identity is ambiguous across Registry jobs: "
                f"{artifact_type}/{artifact_id}"
            )
        candidate = candidates[0]
        if artifact_type != "evidence_manifest" and (
            candidate.dependency_kind != "local_job"
            or candidate.job_id != registry.job_id
        ):
            raise ValidationInputDependencyError(
                f"Validation primary input must belong to the local job: {artifact_id}"
            )
        dependencies.append(candidate)

    verified: list[ArtifactDependencyRefV2] = []
    for dependency in dependencies:
        try:
            if dependency.dependency_kind == "local_job":
                target_record = registry.verify_ready_artifact_closure(
                    dependency,
                    external_registry_resolver=external_registry_resolver,
                )
            else:
                if external_registry_resolver is None:
                    raise RegistryError(
                        "external dependency cannot be verified without a resolver: "
                        f"{dependency.job_id}/{dependency.artifact_id}"
                    )
                target_registry = external_registry_resolver(dependency.job_id)
                if target_registry is None or target_registry.job_id != dependency.job_id:
                    raise RegistryError(
                        "external dependency Registry is unavailable or has the wrong owner: "
                        f"{dependency.job_id}/{dependency.artifact_id}"
                    )
                target_record = target_registry.verify_ready_artifact_closure(
                    ArtifactDependencyRefV2(
                        dependency_kind="local_job",
                        job_id=dependency.job_id,
                        artifact_id=dependency.artifact_id,
                        artifact_type=dependency.artifact_type,
                        path=dependency.path,
                        content_hash=dependency.content_hash,
                    ),
                    external_registry_resolver=external_registry_resolver,
                )
            verified.append(
                ArtifactDependencyRefV2.from_record(
                    target_record,
                    dependency_kind=dependency.dependency_kind,
                )
            )
        except (OSError, RegistryError, TypeError, ValueError) as exc:
            raise ValidationInputDependencyError(
                f"Validation input dependencies are not durably verified: {exc}"
            ) from exc
    if len(verified) != len(identities):
        raise ValidationInputDependencyError(
            "Validation input dependency resolution returned an incomplete closure"
        )
    return verified


def validate_validation_dependency_contract(
    record: ArtifactRecord,
    inputs: ValidationInputArtifactsV1,
) -> None:
    """Require the canonical payload and Registry dependency graph to be identical."""

    declared = declared_validation_input_identities(inputs)
    actual = tuple(
        (dependency.artifact_type, dependency.artifact_id, dependency.content_hash)
        for dependency in record.depends_on
    )
    if Counter(actual) != Counter(declared):
        raise ValidationInputDependencyError(
            "Validation input dependencies do not exactly match the canonical payload"
        )
    fingerprint = inputs.validation_source_authority_fingerprint
    if fingerprint:
        fingerprint_binding_id = str(
            fingerprint.get("current_binding_artifact_id") or ""
        )
        fingerprint_binding_hash = str(
            fingerprint.get("current_binding_content_hash") or ""
        )
        if inputs.validation_source_binding_id and (
            fingerprint_binding_id != inputs.validation_source_binding_id
            or fingerprint_binding_hash != inputs.validation_source_binding_hash
        ):
            raise ValidationInputDependencyError(
                "Validation source authority fingerprint does not match the current binding"
            )
        raw_entries = fingerprint.get("papers")
        if not isinstance(raw_entries, (list, tuple)):
            raise ValidationInputDependencyError(
                "Validation source authority fingerprint papers are invalid"
            )
        fingerprint_evidence = Counter(
            (
                str(item.get("evidence_manifest_id") or ""),
                str(item.get("evidence_manifest_hash") or ""),
            )
            for item in raw_entries
            if isinstance(item, Mapping)
            and str(item.get("evidence_manifest_id") or "")
        )
        declared_evidence = Counter(
            (artifact_id, content_hash)
            for artifact_id, content_hash in zip(
                inputs.evidence_manifest_ids,
                inputs.evidence_manifest_hashes,
            )
        )
        if fingerprint_evidence != declared_evidence:
            raise ValidationInputDependencyError(
                "Validation source authority fingerprint does not match evidence dependencies"
            )
    for dependency in record.depends_on:
        if not dependency.job_id or not dependency.path:
            raise ValidationInputDependencyError(
                "Validation input dependencies require job and path identities"
            )
        if dependency.dependency_kind == "local_job":
            if dependency.job_id != record.job_id:
                raise ValidationInputDependencyError(
                    "Validation input dependencies contain a foreign local-job edge"
                )
        elif dependency.job_id == record.job_id:
            raise ValidationInputDependencyError(
                "Validation input dependencies contain a self-referential external edge"
            )
        if dependency.artifact_type in {"review_draft", "citation_manifest"} and (
            dependency.dependency_kind != "local_job"
            or dependency.job_id != record.job_id
        ):
            raise ValidationInputDependencyError(
                "Validation primary input dependencies must belong to the local job"
            )

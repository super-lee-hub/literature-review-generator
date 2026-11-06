"""Owner-audited materialization of a quarantined Stage 1 correction.

This module records an explicit adoption action and writes a separate derived
summary-set artifact.  The source Registry, canonical Stage 1 pointer, and
quarantined review artifacts remain unchanged.  The derived set is deliberately
not Stage 1 reuse authority until a typed authority contract verifies it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.job_workspace import JobWorkspace, publish_json_artifact, utc_now_iso
from services.summary_correction import (
    CANDIDATE_ARTIFACT_TYPE,
    PROPOSAL_ARTIFACT_TYPE,
    SOURCE_SNAPSHOT_ARTIFACT_TYPE,
    SourceSummaryCorrectionError,
    verify_source_summary_correction_candidate,
)


DERIVED_SUMMARY_SET_ARTIFACT_TYPE = "stage1_derived_summary_set"
DERIVED_SUMMARY_SET_ARTIFACT_VERSION = "owner-corrected-v1"
ADOPTION_RECEIPT_ARTIFACT_TYPE = "stage1_summary_correction_adoption_receipt"
ADOPTION_RECEIPT_ARTIFACT_VERSION = "v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceSummaryCorrectionAdoptionError(ValueError):
    """Raised when an explicit correction adoption cannot be verified safely."""


@dataclass(frozen=True)
class SourceSummaryCorrectionAdoptionResultV1:
    status: str
    action_id: str
    adoption_receipt_artifact_id: str
    adoption_receipt_artifact_hash: str
    derived_summary_artifact_id: str
    derived_summary_artifact_hash: str
    source_summary_artifact_id: str
    source_summary_artifact_hash: str
    candidate_artifact_id: str
    candidate_artifact_hash: str
    summary_count: int
    source_registry_unchanged: bool
    usable_as_stage1_reuse: bool = False
    canonical_pointer_advanced: bool = False
    provider_calls: int = 0
    provider_receipt_ids_created: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["provider_receipt_ids_created"] = list(self.provider_receipt_ids_created)
        return payload


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SourceSummaryCorrectionAdoptionError(message)


def _required_record(record: ArtifactRecord | None, message: str) -> ArtifactRecord:
    if record is None:
        raise SourceSummaryCorrectionAdoptionError(message)
    return record


def _required_mapping(value: object, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceSummaryCorrectionAdoptionError(message)
    return value


def _required_summary_list(value: object, message: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise SourceSummaryCorrectionAdoptionError(message)
    summaries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SourceSummaryCorrectionAdoptionError(
                f"{message}: entry {index} is not an object"
            )
        summaries.append(copy.deepcopy(dict(item)))
    return summaries


def _read_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceSummaryCorrectionAdoptionError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise SourceSummaryCorrectionAdoptionError(f"{label} must be a JSON object")
    return dict(payload)


def _artifact_ref(record: ArtifactRecord) -> dict[str, str]:
    return {
        "job_id": record.job_id,
        "artifact_id": record.artifact_id,
        "artifact_role": record.artifact_role,
        "artifact_type": record.artifact_type,
        "artifact_version": record.artifact_version,
        "status": record.status,
        "path": str(Path(record.path).resolve()),
        "content_hash": record.content_hash,
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _resolve_source_registry(
    source_registry: ArtifactRegistry,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> Callable[[str], ArtifactRegistry | None]:
    def resolve(job_id: str) -> ArtifactRegistry | None:
        if job_id == source_registry.job_id:
            return source_registry
        if external_registry_resolver is None:
            return None
        return external_registry_resolver(job_id)

    return resolve


def _workspace_checks(
    *,
    source_registry: ArtifactRegistry,
    destination_registry: ArtifactRegistry,
    workspace: JobWorkspace,
) -> None:
    _require(
        workspace.job_id == destination_registry.job_id,
        "destination JobWorkspace and Registry job_id differ",
    )
    workspace_registry_path = os.path.normcase(
        os.path.abspath(workspace.paths.registry_path)
    )
    destination_registry_path = os.path.normcase(
        os.path.abspath(destination_registry.registry_path)
    )
    source_registry_path = os.path.normcase(os.path.abspath(source_registry.registry_path))
    _require(
        workspace_registry_path == destination_registry_path,
        "destination Registry does not belong to the destination JobWorkspace",
    )
    _require(
        source_registry_path != destination_registry_path
        and source_registry.job_id != destination_registry.job_id,
        "source and destination Stage 1 Registries must be separate",
    )


def _load_verified_inputs(
    *,
    source_registry: ArtifactRegistry,
    destination_registry: ArtifactRegistry,
    candidate_artifact_id: str,
    expected_candidate_hash: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> tuple[ArtifactRecord, ArtifactRecord, ArtifactRecord, ArtifactRecord, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Verify the quarantined candidate and reconstruct its exact input chain."""

    destination_registry.reload()
    source_registry.reload()
    candidate_record = destination_registry.get(candidate_artifact_id)
    _require(candidate_record is not None, f"correction candidate is missing: {candidate_artifact_id}")
    assert candidate_record is not None
    _require(
        candidate_record.status == "quarantined",
        "source correction candidate must remain quarantined during adoption",
    )
    _require(
        candidate_record.artifact_type == CANDIDATE_ARTIFACT_TYPE,
        "correction candidate artifact type is invalid",
    )
    normalized_expected_hash = str(expected_candidate_hash or "").strip().lower().removeprefix("sha256:")
    _require(
        bool(_SHA256_RE.fullmatch(normalized_expected_hash))
        and candidate_record.content_hash == normalized_expected_hash,
        "expected candidate hash does not match the quarantined Registry record",
    )
    _require(
        file_sha256(candidate_record.path) == candidate_record.content_hash,
        "quarantined candidate bytes do not match the Registry hash",
    )

    try:
        verification = verify_source_summary_correction_candidate(
            source_registry=source_registry,
            destination_registry=destination_registry,
            candidate_artifact_id=candidate_record.artifact_id,
            external_registry_resolver=external_registry_resolver,
        )
    except (SourceSummaryCorrectionError, RegistryError, OSError, ValueError, TypeError) as exc:
        raise SourceSummaryCorrectionAdoptionError(
            f"quarantined source correction candidate failed verification: {exc}"
        ) from exc
    _require(verification.verified, "quarantined source correction candidate is not verified")
    _require(
        verification.usable_as_stage1_reuse is False
        and verification.canonical_pointer_advanced is False,
        "candidate verifier reported an unexpected Stage 1 authority transition",
    )

    candidate_payload = _read_json_object(candidate_record.path, label="candidate artifact")
    source_authority = _required_mapping(
        candidate_payload.get("source_authority"),
        "candidate source authority is missing",
    )
    source_artifact_id = str(source_authority.get("source_artifact_id") or "")
    source_record = _required_record(
        source_registry.get(source_artifact_id),
        "candidate source Stage 1 artifact is missing",
    )
    _require(
        source_record.job_id == source_registry.job_id
        and source_record.status == "ready"
        and source_record.artifact_type == "stage1_canonical_summaries"
        and source_record.artifact_version == "v1",
        "candidate source is not the registered READY Stage 1 canonical summary artifact",
    )
    source_registry.verify_ready_artifact_closure(
        source_record,
        external_registry_resolver=_resolve_source_registry(
            source_registry, external_registry_resolver
        ),
    )
    _require(
        file_sha256(source_record.path) == source_record.content_hash,
        "source Stage 1 summary bytes do not match the Registry hash",
    )
    _require(
        str(source_authority.get("source_artifact_hash") or "") == source_record.content_hash,
        "candidate source artifact hash is stale",
    )

    source_doc = _read_json_object(source_record.path, label="source Stage 1 summary artifact")
    source_summaries = _required_summary_list(
        source_doc.get("summaries"),
        "source Stage 1 summary artifact has no summaries list",
    )
    _require(
        hash_json(source_summaries) == str(source_doc.get("summary_set_hash") or ""),
        "source Stage 1 summary-set hash is invalid",
    )
    candidate_summaries = _required_summary_list(
        candidate_payload.get("candidate_summaries"),
        "candidate has no candidate_summaries list",
    )
    _require(
        len(candidate_summaries) == len(source_summaries)
        and str(candidate_payload.get("candidate_summary_set_hash") or "")
        == hash_json(candidate_summaries),
        "candidate summary set is inconsistent with its declared hash or source cardinality",
    )
    _require(
        hash_json(candidate_summaries) == verification.candidate_summary_set_hash,
        "candidate summary set differs from the verified correction result",
    )

    proposal_id = str(candidate_payload.get("proposal_artifact_id") or "")
    source_snapshot_id = str(candidate_payload.get("source_snapshot_artifact_id") or "")
    proposal_record = _required_record(
        destination_registry.get(proposal_id),
        "candidate proposal artifact is missing",
    )
    source_snapshot_record = _required_record(
        destination_registry.get(source_snapshot_id),
        "candidate source snapshot artifact is missing",
    )
    _require(
        proposal_record.status == "quarantined"
        and proposal_record.artifact_type == PROPOSAL_ARTIFACT_TYPE
        and source_snapshot_record.status == "quarantined"
        and source_snapshot_record.artifact_type == SOURCE_SNAPSHOT_ARTIFACT_TYPE,
        "proposal and source snapshot must remain quarantined review artifacts",
    )
    for record, label in (
        (proposal_record, "proposal"),
        (source_snapshot_record, "source snapshot"),
    ):
        _require(
            file_sha256(record.path) == record.content_hash,
            f"quarantined {label} bytes do not match the Registry hash",
        )

    proposal_payload = _read_json_object(proposal_record.path, label="proposal artifact")
    normalized_proposal = _required_mapping(
        proposal_payload.get("normalized_proposal"),
        "proposal has no normalized proposal",
    )
    normalized_proposal_hash = hash_json(normalized_proposal)
    _require(
        normalized_proposal_hash
        == str(proposal_payload.get("normalized_proposal_hash") or "")
        == str(candidate_payload.get("normalized_proposal_hash") or ""),
        "normalized proposal hash does not match the candidate and proposal artifacts",
    )
    return (
        source_record,
        source_snapshot_record,
        proposal_record,
        candidate_record,
        source_doc,
        proposal_payload,
        copy.deepcopy(candidate_summaries),
    )


def _verify_existing_artifact(
    *,
    registry: ArtifactRegistry,
    artifact_id: str,
    artifact_type: str,
    artifact_version: str,
    expected_hash: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None],
    expected_dependencies: list[ArtifactDependencyRefV2],
) -> ArtifactRecord | None:
    registry.reload()
    record = registry.get(artifact_id)
    if record is None:
        return None
    _require(record.status == "ready", f"existing adoption artifact is not READY: {artifact_id}")
    _require(
        record.artifact_type == artifact_type and record.artifact_version == artifact_version,
        f"existing adoption artifact has an unexpected type/version: {artifact_id}",
    )
    _require(
        record.content_hash == expected_hash and file_sha256(record.path) == expected_hash,
        f"existing adoption artifact bytes do not match the expected content: {artifact_id}",
    )
    _require(
        [item.to_dict() for item in record.depends_on]
        == [item.to_dict() for item in expected_dependencies],
        f"existing adoption artifact dependencies do not match the expected source closure: {artifact_id}",
    )
    registry.verify_ready_artifact_closure(
        record,
        external_registry_resolver=external_registry_resolver,
    )
    return record


def _validate_existing_receipt(
    record: ArtifactRecord,
    *,
    expected_without_timestamp: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _read_json_object(record.path, label="existing adoption receipt")
    comparison = dict(payload)
    created_at = comparison.pop("created_at", None)
    _require(bool(str(created_at or "").strip()), "existing adoption receipt has no created_at")
    _require(
        comparison == dict(expected_without_timestamp),
        "existing adoption receipt does not match the requested owner action and source bindings",
    )
    return payload


def adopt_source_summary_correction_candidate(
    *,
    source_registry: ArtifactRegistry,
    destination_registry: ArtifactRegistry,
    workspace: JobWorkspace,
    publication_context: Any,
    candidate_artifact_id: str,
    expected_candidate_hash: str,
    actor: str,
    reason: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> SourceSummaryCorrectionAdoptionResultV1:
    """Record a manual adoption and materialize a separate derived summary set.

    Every input is verified before publication.  Candidate, proposal, source
    snapshot, original Stage 1 bytes, and both current pointers are left in
    their existing states.  The resulting derived artifact and receipt are
    intentionally not typed Stage 1 reuse authority.
    """

    actor_value = str(actor or "").strip()
    reason_value = str(reason or "").strip()
    _require(bool(actor_value), "manual source correction adoption requires an actor")
    _require(bool(reason_value), "manual source correction adoption requires a reason")
    _workspace_checks(
        source_registry=source_registry,
        destination_registry=destination_registry,
        workspace=workspace,
    )
    effective_resolver = _resolve_source_registry(source_registry, external_registry_resolver)

    source_registry.reload()
    destination_registry.reload()
    source_registry_path = Path(source_registry.registry_path)
    source_registry_file_hash_before = file_sha256(source_registry_path)
    source_registry_revision_before = source_registry.revision
    source_record, snapshot_record, proposal_record, candidate_record, source_doc, proposal_payload, candidate_summaries = (
        _load_verified_inputs(
            source_registry=source_registry,
            destination_registry=destination_registry,
            candidate_artifact_id=candidate_artifact_id,
            expected_candidate_hash=expected_candidate_hash,
            external_registry_resolver=effective_resolver,
        )
    )
    normalized_expected_hash = str(expected_candidate_hash).strip().lower().removeprefix("sha256:")
    source_registry_file_hash_after_preflight = file_sha256(source_registry_path)
    _require(
        source_registry_file_hash_after_preflight == source_registry_file_hash_before
        and source_registry.revision == source_registry_revision_before,
        "source Registry changed during correction-adoption preflight",
    )
    source_summary_file_hash_before = file_sha256(source_record.path)
    _require(
        source_summary_file_hash_before == source_record.content_hash,
        "source Stage 1 summary changed during correction-adoption preflight",
    )
    candidate_payload = _read_json_object(candidate_record.path, label="candidate artifact")
    normalized_proposal = copy.deepcopy(dict(proposal_payload["normalized_proposal"]))
    normalized_proposal_hash = hash_json(normalized_proposal)
    candidate_summary_set_hash = hash_json(candidate_summaries)
    source_summary_set_hash = str(source_doc.get("summary_set_hash") or "")
    source_authority = candidate_payload.get("source_authority")
    _require(isinstance(source_authority, Mapping), "candidate source authority is missing")

    action_id = hash_json(
        {
            "action": "manual_source_summary_correction_adoption_v1",
            "source_artifact_id": source_record.artifact_id,
            "source_artifact_hash": source_record.content_hash,
            "candidate_artifact_id": candidate_record.artifact_id,
            "candidate_artifact_hash": normalized_expected_hash,
        }
    )[:32]
    derived_payload: dict[str, Any] = {
        "artifact_type": DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
        "artifact_version": DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
        "job_id": destination_registry.job_id,
        "status": "derived_summary_set_ready_not_reuse_authority",
        "authority_kind": "owner_approved_source_correction",
        "owner_action_id": action_id,
        "source_summary_artifact_id": source_record.artifact_id,
        "source_summary_artifact_hash": source_record.content_hash,
        "source_summary_set_hash": source_summary_set_hash,
        "source_identity_list_hash": str(candidate_payload.get("source_identity_list_hash") or ""),
        "candidate_artifact_id": candidate_record.artifact_id,
        "candidate_artifact_hash": candidate_record.content_hash,
        "proposal_artifact_id": proposal_record.artifact_id,
        "proposal_artifact_hash": proposal_record.content_hash,
        "candidate_summary_set_hash": candidate_summary_set_hash,
        "summary_count": len(candidate_summaries),
        "identity_set_preserved": candidate_payload.get("identity_set_preserved") is True,
        "summaries": copy.deepcopy(candidate_summaries),
        "usable_as_stage1_reuse": False,
        "canonical_pointer_advanced": False,
        "provider_calls": 0,
        "provider_receipt_ids_created": [],
    }
    derived_bytes_hash = hashlib.sha256(_json_bytes(derived_payload)).hexdigest()
    derived_artifact_id = f"stage1:derived_summary_set:{derived_bytes_hash[:32]}"
    source_dependency = ArtifactDependencyRefV2.from_record(
        source_record,
        dependency_kind="external_job",
    )
    source_registry_ref = _artifact_ref(source_record)
    derived_dependencies = [source_dependency]
    source_registry_identity = {
        "job_id": source_registry.job_id,
        "registry_path": str(Path(source_registry.registry_path).resolve()),
        "registry_file_sha256": source_registry_file_hash_before,
        "registry_revision": str(source_registry_revision_before),
    }
    action_owner = {
        "method": "manual_operator_action",
        "actor": actor_value,
        "reason": reason_value,
        "expected_candidate_sha256": normalized_expected_hash,
        "identity_assurance": "actor_string_recorded_not_cryptographically_verified",
    }
    derived_target_path = str(Path(workspace.artifact_path(
        f"stage1_summary_correction/adoptions/{action_id}/derived_summary_set.json"
    )).resolve())
    receipt_without_timestamp: dict[str, Any] = {
        "artifact_type": ADOPTION_RECEIPT_ARTIFACT_TYPE,
        "artifact_version": ADOPTION_RECEIPT_ARTIFACT_VERSION,
        "job_id": destination_registry.job_id,
        "status": "owner_action_recorded_for_derived_summary",
        "action_id": action_id,
        "owner_action": action_owner,
        "source_registry": source_registry_identity,
        "source_summary": source_registry_ref,
        "source_snapshot": _artifact_ref(snapshot_record),
        "proposal": _artifact_ref(proposal_record),
        "candidate": _artifact_ref(candidate_record),
        "normalized_proposal": normalized_proposal,
        "normalized_proposal_hash": normalized_proposal_hash,
        "source_summary_set_hash": source_summary_set_hash,
        "candidate_summary_set_hash": candidate_summary_set_hash,
        "source_identity_list_hash": str(candidate_payload.get("source_identity_list_hash") or ""),
        "candidate_identity_list_hash": str(candidate_payload.get("candidate_identity_list_hash") or ""),
        "derived_summary": {
            "artifact_id": derived_artifact_id,
            "artifact_type": DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
            "artifact_version": DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
            "path": derived_target_path,
            "content_hash": derived_bytes_hash,
            "summary_count": len(candidate_summaries),
        },
        "identity_set_preserved": candidate_payload.get("identity_set_preserved") is True,
        "source_registry_unchanged": True,
        "usable_as_stage1_reuse": False,
        "canonical_pointer_advanced": False,
        "provider_calls": 0,
        "provider_receipt_ids_created": [],
    }
    receipt_artifact_id = f"stage1:summary_correction_adoption:{action_id}"
    destination_registry.reload()
    receipt_record = destination_registry.get(receipt_artifact_id)
    had_existing_receipt = receipt_record is not None

    # Re-run all source/candidate checks immediately before each publication.
    # The first immutable derived file is not reusable authority without its
    # separate receipt; a failed second publication can safely be retried.
    _load_verified_inputs(
        source_registry=source_registry,
        destination_registry=destination_registry,
        candidate_artifact_id=candidate_artifact_id,
        expected_candidate_hash=normalized_expected_hash,
        external_registry_resolver=effective_resolver,
    )
    _require(
        file_sha256(source_registry_path) == source_registry_file_hash_before
        and source_registry.revision == source_registry_revision_before,
        "source Registry changed before derived Stage 1 summary publication",
    )
    derived_record = _verify_existing_artifact(
        registry=destination_registry,
        artifact_id=derived_artifact_id,
        artifact_type=DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
        artifact_version=DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
        expected_hash=derived_bytes_hash,
        external_registry_resolver=effective_resolver,
        expected_dependencies=derived_dependencies,
    )
    if derived_record is None:
        derived_record = publish_json_artifact(
            publication_context,
            destination_registry,
            workspace.artifact_path(
                f"stage1_summary_correction/adoptions/{action_id}/derived_summary_set.json"
            ),
            derived_payload,
            artifact_id=derived_artifact_id,
            artifact_role="stage1_summary_correction_derived",
            artifact_type=DERIVED_SUMMARY_SET_ARTIFACT_TYPE,
            artifact_version=DERIVED_SUMMARY_SET_ARTIFACT_VERSION,
            producer="services.summary_correction_adoption.adopt_source_summary_correction_candidate",
            status="ready",
            depends_on=[source_dependency],
            external_registry_resolver=effective_resolver,
            metadata={
                "authority_kind": "owner_approved_source_correction",
                "owner_action_id": action_id,
                "source_summary_artifact_id": source_record.artifact_id,
                "source_summary_artifact_hash": source_record.content_hash,
                "candidate_artifact_id": candidate_record.artifact_id,
                "candidate_artifact_hash": candidate_record.content_hash,
                "usable_as_stage1_reuse": False,
            },
        )
    _require(
        derived_record.content_hash == derived_bytes_hash
        and file_sha256(derived_record.path) == derived_bytes_hash,
        "published derived summary bytes do not match the precomputed content hash",
    )
    receipt_without_timestamp["derived_summary"] = {
        **_artifact_ref(derived_record),
        "summary_count": len(candidate_summaries),
    }

    if receipt_record is not None:
        _require(receipt_record.status == "ready", "existing adoption receipt is not READY")
        _require(
            receipt_record.artifact_type == ADOPTION_RECEIPT_ARTIFACT_TYPE
            and receipt_record.artifact_version == ADOPTION_RECEIPT_ARTIFACT_VERSION,
            "existing adoption receipt has an unexpected type/version",
        )
        _require(
            file_sha256(receipt_record.path) == receipt_record.content_hash,
            "existing adoption receipt bytes do not match its Registry hash",
        )
        existing_receipt_payload = _validate_existing_receipt(
            receipt_record,
            expected_without_timestamp=receipt_without_timestamp,
        )
        receipt_created_at = str(existing_receipt_payload["created_at"])
    else:
        receipt_created_at = utc_now_iso()

    # The final receipt references the published immutable path.  It is the
    # only artifact in this transaction that records the operator action.
    receipt_payload = {**receipt_without_timestamp, "created_at": receipt_created_at}
    receipt_dependencies = [
        ArtifactDependencyRefV2.from_record(derived_record),
        source_dependency,
    ]
    if receipt_record is None:
        _load_verified_inputs(
            source_registry=source_registry,
            destination_registry=destination_registry,
            candidate_artifact_id=candidate_artifact_id,
            expected_candidate_hash=normalized_expected_hash,
            external_registry_resolver=effective_resolver,
        )
        _require(
            file_sha256(source_registry_path) == source_registry_file_hash_before
            and source_registry.revision == source_registry_revision_before,
            "source Registry changed before adoption receipt publication",
        )
        try:
            receipt_record = publish_json_artifact(
                publication_context,
                destination_registry,
                workspace.artifact_path(
                    f"stage1_summary_correction/adoptions/{action_id}/adoption_receipt.json"
                ),
                receipt_payload,
                artifact_id=receipt_artifact_id,
                artifact_role="stage1_summary_correction_adoption_receipt",
                artifact_type=ADOPTION_RECEIPT_ARTIFACT_TYPE,
                artifact_version=ADOPTION_RECEIPT_ARTIFACT_VERSION,
                producer="services.summary_correction_adoption.adopt_source_summary_correction_candidate",
                status="ready",
                depends_on=receipt_dependencies,
                external_registry_resolver=effective_resolver,
                metadata={
                    "owner_action_id": action_id,
                    "derived_summary_artifact_id": derived_record.artifact_id,
                    "derived_summary_artifact_hash": derived_record.content_hash,
                    "usable_as_stage1_reuse": False,
                },
            )
        except (OSError, RegistryError, ValueError, TypeError, RuntimeError):
            # A concurrent identical action may have published the same
            # content-addressed receipt first.  Accept only its exact action
            # and output bindings; a conflicting same-action receipt remains
            # a hard failure.
            destination_registry.reload()
            concurrent_receipt = destination_registry.get(receipt_artifact_id)
            if concurrent_receipt is None:
                raise
            receipt_record = concurrent_receipt
            _require(receipt_record.status == "ready", "concurrent adoption receipt is not READY")
            _require(
                receipt_record.artifact_type == ADOPTION_RECEIPT_ARTIFACT_TYPE
                and receipt_record.artifact_version == ADOPTION_RECEIPT_ARTIFACT_VERSION
                and file_sha256(receipt_record.path) == receipt_record.content_hash,
                "concurrent adoption receipt identity or bytes are invalid",
            )
            _validate_existing_receipt(
                receipt_record,
                expected_without_timestamp=receipt_without_timestamp,
            )
    _require(
        receipt_record.artifact_id == receipt_artifact_id
        and receipt_record.status == "ready"
        and receipt_record.content_hash == file_sha256(receipt_record.path),
        "adoption receipt readback did not match its registered bytes",
    )
    destination_registry.reload()
    receipt_record = destination_registry.get(receipt_artifact_id)
    derived_record = destination_registry.get(derived_artifact_id)
    _require(receipt_record is not None and derived_record is not None, "adoption outputs disappeared after publication")
    assert receipt_record is not None and derived_record is not None
    verification = verify_source_summary_correction_adoption(
        source_registry=source_registry,
        destination_registry=destination_registry,
        adoption_receipt_artifact_id=receipt_record.artifact_id,
        external_registry_resolver=effective_resolver,
    )
    _require(verification.verified, "adoption receipt readback verification failed")
    source_registry.reload()
    source_registry_unchanged = bool(
        file_sha256(source_registry_path) == source_registry_file_hash_before
        and source_registry.revision == source_registry_revision_before
        and file_sha256(source_record.path) == source_summary_file_hash_before
    )
    _require(source_registry_unchanged, "source Registry or canonical Stage 1 summary changed during adoption")
    return SourceSummaryCorrectionAdoptionResultV1(
        status="already_adopted" if had_existing_receipt else "owner_approved_derived_summary_ready",
        action_id=action_id,
        adoption_receipt_artifact_id=receipt_record.artifact_id,
        adoption_receipt_artifact_hash=receipt_record.content_hash,
        derived_summary_artifact_id=derived_record.artifact_id,
        derived_summary_artifact_hash=derived_record.content_hash,
        source_summary_artifact_id=source_record.artifact_id,
        source_summary_artifact_hash=source_record.content_hash,
        candidate_artifact_id=candidate_record.artifact_id,
        candidate_artifact_hash=candidate_record.content_hash,
        summary_count=len(candidate_summaries),
        source_registry_unchanged=source_registry_unchanged,
    )


@dataclass(frozen=True)
class SourceSummaryCorrectionAdoptionVerificationV1:
    verified: bool
    adoption_receipt_artifact_id: str
    derived_summary_artifact_id: str
    source_summary_artifact_id: str
    candidate_artifact_id: str
    summary_count: int
    usable_as_stage1_reuse: bool = False
    canonical_pointer_advanced: bool = False


def verify_source_summary_correction_adoption(
    *,
    source_registry: ArtifactRegistry,
    destination_registry: ArtifactRegistry,
    adoption_receipt_artifact_id: str,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> SourceSummaryCorrectionAdoptionVerificationV1:
    """Reverify an adoption receipt, its derived bytes, and source lineage."""

    source_registry.reload()
    destination_registry.reload()
    receipt_record = destination_registry.get(adoption_receipt_artifact_id)
    _require(receipt_record is not None, "source correction adoption receipt is missing")
    assert receipt_record is not None
    _require(
        receipt_record.status == "ready"
        and receipt_record.artifact_type == ADOPTION_RECEIPT_ARTIFACT_TYPE
        and receipt_record.artifact_version == ADOPTION_RECEIPT_ARTIFACT_VERSION,
        "source correction adoption receipt has an invalid Registry identity",
    )
    _require(
        file_sha256(receipt_record.path) == receipt_record.content_hash,
        "source correction adoption receipt bytes do not match the Registry hash",
    )
    receipt_payload = _read_json_object(receipt_record.path, label="source correction adoption receipt")
    _require(receipt_payload.get("artifact_type") == ADOPTION_RECEIPT_ARTIFACT_TYPE, "adoption receipt payload type mismatch")
    _require(receipt_payload.get("artifact_version") == ADOPTION_RECEIPT_ARTIFACT_VERSION, "adoption receipt payload version mismatch")
    _require(receipt_payload.get("job_id") == destination_registry.job_id, "adoption receipt job ID mismatch")
    _require(receipt_payload.get("usable_as_stage1_reuse") is False, "adoption receipt cannot claim Stage 1 reuse authority")
    _require(receipt_payload.get("canonical_pointer_advanced") is False, "adoption receipt claims a canonical pointer update")
    _require(receipt_payload.get("provider_calls") == 0, "adoption receipt claims provider calls")
    _require(receipt_payload.get("provider_receipt_ids_created") == [], "adoption receipt invents provider receipts")

    source_ref = _required_mapping(
        receipt_payload.get("source_summary"),
        "adoption receipt source summary reference is missing",
    )
    candidate_ref = _required_mapping(
        receipt_payload.get("candidate"),
        "adoption receipt candidate reference is missing",
    )
    proposal_ref = _required_mapping(
        receipt_payload.get("proposal"),
        "adoption receipt proposal reference is missing",
    )
    snapshot_ref = _required_mapping(
        receipt_payload.get("source_snapshot"),
        "adoption receipt source snapshot reference is missing",
    )
    derived_ref = _required_mapping(
        receipt_payload.get("derived_summary"),
        "adoption receipt derived summary reference is missing",
    )
    owner_action = _required_mapping(
        receipt_payload.get("owner_action"),
        "adoption receipt owner action is missing",
    )
    actor = str(owner_action.get("actor") or "").strip()
    reason = str(owner_action.get("reason") or "").strip()
    _require(bool(actor) and bool(reason), "adoption receipt actor or reason is missing")
    _require(owner_action.get("method") == "manual_operator_action", "adoption receipt is not a manual action")
    _require(
        owner_action.get("identity_assurance") == "actor_string_recorded_not_cryptographically_verified",
        "adoption receipt identity assurance label is invalid",
    )

    source_artifact_id = str(source_ref.get("artifact_id") or "")
    source_record = _required_record(
        source_registry.get(source_artifact_id),
        "adoption receipt source summary is not registered",
    )
    _require(
        _artifact_ref(source_record) == dict(source_ref),
        "adoption receipt source summary reference is stale",
    )
    source_registry_identity = _required_mapping(
        receipt_payload.get("source_registry"),
        "adoption receipt source Registry binding is missing",
    )
    _require(
        str(source_registry_identity.get("job_id") or "") == source_registry.job_id
        and str(source_registry_identity.get("registry_path") or "")
        == str(Path(source_registry.registry_path).resolve())
        and _SHA256_RE.fullmatch(str(source_registry_identity.get("registry_file_sha256") or "")) is not None
        and bool(str(source_registry_identity.get("registry_revision") or "")),
        "adoption receipt source Registry identity or original fence is invalid",
    )

    normalized_candidate_hash = str(owner_action.get("expected_candidate_sha256") or "")
    candidate_artifact_id = str(candidate_ref.get("artifact_id") or "")
    try:
        verify_source_summary_correction_candidate(
            source_registry=source_registry,
            destination_registry=destination_registry,
            candidate_artifact_id=candidate_artifact_id,
            external_registry_resolver=external_registry_resolver,
        )
    except (SourceSummaryCorrectionError, RegistryError, OSError, ValueError, TypeError) as exc:
        raise SourceSummaryCorrectionAdoptionError(
            f"adopted correction candidate no longer verifies: {exc}"
        ) from exc
    candidate_record = _required_record(
        destination_registry.get(candidate_artifact_id),
        "adoption candidate is not registered",
    )
    proposal_record = _required_record(
        destination_registry.get(str(proposal_ref.get("artifact_id") or "")),
        "adoption proposal is not registered",
    )
    snapshot_record = _required_record(
        destination_registry.get(str(snapshot_ref.get("artifact_id") or "")),
        "adoption source snapshot is not registered",
    )
    _require(
        candidate_record.content_hash == normalized_candidate_hash
        and _artifact_ref(candidate_record) == dict(candidate_ref)
        and _artifact_ref(proposal_record) == dict(proposal_ref)
        and _artifact_ref(snapshot_record) == dict(snapshot_ref),
        "adoption receipt candidate/proposal/source snapshot bindings are stale",
    )
    expected_action_id = hash_json(
        {
            "action": "manual_source_summary_correction_adoption_v1",
            "source_artifact_id": source_record.artifact_id,
            "source_artifact_hash": source_record.content_hash,
            "candidate_artifact_id": candidate_record.artifact_id,
            "candidate_artifact_hash": candidate_record.content_hash,
        }
    )[:32]
    _require(
        str(receipt_payload.get("action_id") or "") == expected_action_id
        and receipt_record.artifact_id == f"stage1:summary_correction_adoption:{expected_action_id}",
        "adoption receipt action identity does not match its source and candidate bindings",
    )
    proposal_payload = _read_json_object(proposal_record.path, label="adoption proposal")
    normalized_proposal = _required_mapping(
        proposal_payload.get("normalized_proposal"),
        "adoption proposal has no normalized proposal",
    )
    _require(
        dict(normalized_proposal) == receipt_payload.get("normalized_proposal")
        and hash_json(normalized_proposal)
        == str(receipt_payload.get("normalized_proposal_hash") or ""),
        "adoption receipt normalized proposal does not match the quarantined proposal bytes",
    )

    derived_artifact_id = str(derived_ref.get("artifact_id") or "")
    derived_record = _required_record(
        destination_registry.get(derived_artifact_id),
        "adoption receipt derived summary artifact is missing",
    )
    _require(
        derived_record.status == "ready"
        and derived_record.artifact_type == DERIVED_SUMMARY_SET_ARTIFACT_TYPE
        and derived_record.artifact_version == DERIVED_SUMMARY_SET_ARTIFACT_VERSION
        and derived_record.content_hash == str(derived_ref.get("content_hash") or "")
        and _artifact_ref(derived_record)
        == {key: value for key, value in dict(derived_ref).items() if key != "summary_count"},
        "adoption receipt derived summary Registry binding is invalid",
    )
    _require(
        file_sha256(derived_record.path) == derived_record.content_hash,
        "derived summary bytes do not match their Registry hash",
    )
    derived_payload = _read_json_object(derived_record.path, label="derived summary artifact")
    candidate_payload = _read_json_object(candidate_record.path, label="quarantined correction candidate")
    candidate_summaries = _required_summary_list(
        candidate_payload.get("candidate_summaries"),
        "quarantined candidate summaries are missing",
    )
    _require(
        derived_payload.get("artifact_type") == DERIVED_SUMMARY_SET_ARTIFACT_TYPE
        and derived_payload.get("artifact_version") == DERIVED_SUMMARY_SET_ARTIFACT_VERSION
        and derived_payload.get("job_id") == destination_registry.job_id
        and derived_payload.get("authority_kind") == "owner_approved_source_correction"
        and derived_payload.get("owner_action_id") == receipt_payload.get("action_id")
        and derived_payload.get("source_summary_artifact_id") == source_record.artifact_id
        and derived_payload.get("source_summary_artifact_hash") == source_record.content_hash
        and derived_payload.get("candidate_artifact_id") == candidate_record.artifact_id
        and derived_payload.get("candidate_artifact_hash") == candidate_record.content_hash
        and derived_payload.get("proposal_artifact_id") == proposal_record.artifact_id
        and derived_payload.get("proposal_artifact_hash") == proposal_record.content_hash
        and derived_payload.get("source_summary_set_hash")
        == str(_read_json_object(source_record.path, label="source Stage 1 summary artifact").get("summary_set_hash") or "")
        and derived_payload.get("summaries") == candidate_summaries
        and hash_json(candidate_summaries)
        == str(derived_payload.get("candidate_summary_set_hash") or "")
        == str(receipt_payload.get("candidate_summary_set_hash") or "")
        and derived_payload.get("summary_count") == len(candidate_summaries)
        and derived_payload.get("identity_set_preserved") is True
        and derived_payload.get("usable_as_stage1_reuse") is False
        and derived_payload.get("canonical_pointer_advanced") is False
        and derived_payload.get("provider_receipt_ids_created") == [],
        "derived summary artifact does not match the verified quarantined candidate",
    )
    _require(
        str(derived_ref.get("path") or "") == str(Path(derived_record.path).resolve())
        and str(derived_ref.get("content_hash") or "") == derived_record.content_hash,
        "adoption receipt derived summary file reference is stale",
    )
    resolver = _resolve_source_registry(source_registry, external_registry_resolver)
    expected_source_dependency = ArtifactDependencyRefV2.from_record(
        source_record,
        dependency_kind="external_job",
    )
    _require(
        [item.to_dict() for item in derived_record.depends_on]
        == [expected_source_dependency.to_dict()],
        "derived summary artifact does not directly depend on the original READY source",
    )
    expected_receipt_dependencies = [
        ArtifactDependencyRefV2.from_record(derived_record),
        expected_source_dependency,
    ]
    _require(
        [item.to_dict() for item in receipt_record.depends_on]
        == [item.to_dict() for item in expected_receipt_dependencies],
        "adoption receipt does not bind the derived artifact and original READY source dependencies",
    )
    try:
        destination_registry.verify_ready_artifact_closure(
            derived_record,
            external_registry_resolver=resolver,
        )
        destination_registry.verify_ready_artifact_closure(
            receipt_record,
            external_registry_resolver=resolver,
        )
    except (OSError, RegistryError, ValueError, TypeError) as exc:
        raise SourceSummaryCorrectionAdoptionError(
            f"adoption Registry dependency closure failed: {exc}"
        ) from exc
    return SourceSummaryCorrectionAdoptionVerificationV1(
        verified=True,
        adoption_receipt_artifact_id=receipt_record.artifact_id,
        derived_summary_artifact_id=derived_record.artifact_id,
        source_summary_artifact_id=source_record.artifact_id,
        candidate_artifact_id=candidate_record.artifact_id,
        summary_count=len(candidate_summaries),
    )


__all__ = [
    "ADOPTION_RECEIPT_ARTIFACT_TYPE",
    "ADOPTION_RECEIPT_ARTIFACT_VERSION",
    "DERIVED_SUMMARY_SET_ARTIFACT_TYPE",
    "DERIVED_SUMMARY_SET_ARTIFACT_VERSION",
    "SourceSummaryCorrectionAdoptionError",
    "SourceSummaryCorrectionAdoptionResultV1",
    "SourceSummaryCorrectionAdoptionVerificationV1",
    "adopt_source_summary_correction_candidate",
    "verify_source_summary_correction_adoption",
]

"""Registry-backed discovery of exact Stage 1 reusable summaries.

This module only inventories typed candidates. Stage1AnalysisService remains
responsible for comparing each candidate against the current source and for
creating reuse evidence or generating a replacement.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.provider_runtime import hash_json
from services.artifact_registry import (
    ArtifactRecord,
    ArtifactRegistry,
    RegistryError,
    file_sha256,
)
from services.stage1_reuse import (
    Stage1ReusableSummaryBindingV1,
    Stage1TypedManifestAuthorityV2,
    verify_stage1_typed_manifest_authority,
)
from summary_schema import normalize_ai_summary

_OWNER_BINDING_FIELDS = frozenset(
    {
        "source_authority_job_id",
        "source_authority_registry_id",
        "source_authority_registry_revision",
        "source_authority_artifact_id",
        "source_authority_artifact_hash",
        "source_summary_manifest_id",
        "source_summary_manifest_hash",
        "source_provider_receipt_closure_id",
        "source_provider_receipt_closure_hash",
        "source_provider_receipt_ledger_id",
        "source_provider_receipt_ledger_hash",
    }
)


@dataclass(frozen=True)
class AutomaticStage1ReuseCandidate:
    canonical_paper_key: str
    summary: Mapping[str, Any]
    owner_job_id: str
    owner_registry_path: str
    owner_registry_id: str
    owner_registry_revision: int
    manifest_record_id: str
    manifest_record_hash: str
    manifest_record_path: str
    manifest_file_hash: str
    manifest_record_version: str
    validation_source_manifests: tuple[dict[str, str], ...]
    superseded_prior_v1_identity: tuple[str, str, str, str] | None
    equivalence_hash: str
    record: ArtifactRecord

    def source_item_metadata(self) -> dict[str, Any]:
        """Return audit metadata bound to the Registry record selected above."""

        return {
            "owner_job_id": self.owner_job_id,
            "owner_registry_id": self.owner_registry_id,
            "owner_registry_revision": self.owner_registry_revision,
            "owner_registry_path": self.owner_registry_path,
            "manifest_artifact_id": self.manifest_record_id,
            "manifest_artifact_hash": self.manifest_record_hash,
            "manifest_path": self.manifest_record_path,
            "manifest_file_sha256": self.manifest_file_hash,
            "manifest_artifact_version": self.manifest_record_version,
            "validation_source_manifests": [dict(item) for item in self.validation_source_manifests],
            "equivalence_hash": self.equivalence_hash,
        }


@dataclass(frozen=True)
class AutomaticStage1ReusePlan:
    summaries: tuple[dict[str, Any], ...]
    candidates_by_key: Mapping[str, AutomaticStage1ReuseCandidate]
    rejected_candidates: tuple[dict[str, Any], ...]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _manifest_reference_identity(value: Any) -> tuple[str, str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    job_id = str(value.get("job_id") or "").strip()
    artifact_id = str(value.get("artifact_id") or "").strip()
    content_hash = str(value.get("content_hash") or "").strip()
    raw_path = str(value.get("path") or "").strip()
    if not job_id or not artifact_id or len(content_hash) != 64 or not raw_path:
        return None
    return job_id, artifact_id, content_hash, str(Path(raw_path).expanduser().resolve())


def _manifest_body(path: Path) -> Mapping[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise TypeError("manifest root is not an object")
    body = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
    if not isinstance(body, Mapping):
        raise TypeError("manifest body is not an object")
    return body


def _registered_v2_target_keys(
    owner: ArtifactRegistry,
    record: ArtifactRecord,
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None,
) -> set[str]:
    """Derive V2 affected keys from immutable Registry-linked correction proof.

    The V2 manifest body cannot identify its own affected key until its bytes
    match the Registry hash. Standard owner-correction exports link the V2
    record to both the approved adoption receipt and the exact prior V1. Those
    registered dependencies provide a conservative per-key target before the
    manifest body is trusted.
    """

    keys: set[str] = set()
    metadata_key = str(record.metadata.get("canonical_paper_key") or "").strip()
    if metadata_key:
        keys.add(metadata_key)

    for dependency in record.depends_on:
        dependency_job_id = str(dependency.job_id or "").strip()
        dependency_artifact_id = str(dependency.artifact_id or "").strip()
        dependency_type = str(dependency.artifact_type or "").strip()
        dependency_hash = str(dependency.content_hash or "").strip()
        dependency_path = str(dependency.path or "").strip()
        if (
            not dependency_job_id
            or not dependency_artifact_id
            or len(dependency_hash) != 64
            or not dependency_path
        ):
            continue

        dependency_registry: ArtifactRegistry | None
        if dependency_job_id == owner.job_id:
            dependency_registry = owner
        else:
            dependency_registry = (
                external_registry_resolver(dependency_job_id)
                if external_registry_resolver is not None
                else None
            )
        if dependency_registry is None:
            continue
        try:
            dependency_registry.reload()
            dependency_record = dependency_registry.get(dependency_artifact_id)
            if (
                dependency_record is None
                or dependency_record.job_id != dependency_job_id
                or dependency_record.status != "ready"
                or dependency_record.artifact_type != dependency_type
                or dependency_record.content_hash != dependency_hash
                or Path(dependency_record.path).resolve()
                != Path(dependency_path).expanduser().resolve()
                or file_sha256(dependency_record.path) != dependency_hash
            ):
                continue
            dependency_payload = _manifest_body(Path(dependency_record.path))
        except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError, RuntimeError):
            continue

        if (
            dependency_type == "stage1_summary_correction_adoption_receipt"
            and dependency_record.artifact_version == "v1"
            and dependency_record.status == "ready"
            and dependency_payload.get("artifact_type") == dependency_type
            and str(dependency_payload.get("artifact_version") or "") == "v1"
            and str(dependency_payload.get("job_id") or "") == dependency_job_id
        ):
            proposal = dependency_payload.get("normalized_proposal")
            proposal = proposal if isinstance(proposal, Mapping) else {}
            proposal_hash = str(dependency_payload.get("normalized_proposal_hash") or "")
            target_key = str(proposal.get("target_canonical_paper_key") or "").strip()
            owner_action = dependency_payload.get("owner_action")
            owner_action = owner_action if isinstance(owner_action, Mapping) else {}
            expected_action_id = str(record.metadata.get("owner_action_id") or "").strip()
            observed_action_id = str(dependency_payload.get("action_id") or "").strip()
            if (
                target_key
                and proposal_hash == hash_json(proposal)
                and expected_action_id
                and observed_action_id == expected_action_id
                and owner_action.get("method") == "manual_operator_action"
                and bool(str(owner_action.get("actor") or "").strip())
                and bool(str(owner_action.get("reason") or "").strip())
                and dependency_payload.get("usable_as_stage1_reuse") is False
                and dependency_payload.get("canonical_pointer_advanced") is False
            ):
                keys.add(target_key)
        elif (
            dependency_type == "stage1_reusable_summary_manifest"
            and dependency_record.artifact_version == "v1"
            and dependency_job_id != owner.job_id
            and dependency_payload.get("artifact_type") == dependency_type
            and str(dependency_payload.get("artifact_version") or "") == "v1"
            and str(dependency_payload.get("job_id") or "") == dependency_job_id
        ):
            prior_key = str(dependency_payload.get("canonical_paper_key") or "").strip()
            if prior_key:
                keys.add(prior_key)
    return keys


def _equivalence_hash(
    summary: Mapping[str, Any],
    binding: Stage1ReusableSummaryBindingV1,
) -> str:
    projection = binding.comparison_projection()
    for field_name in _OWNER_BINDING_FIELDS:
        projection.pop(field_name, None)
    payload = normalize_ai_summary(summary.get("ai_summary"))
    qualification = binding.visual_evidence_qualification
    qualification_projection = (
        dict(qualification) if isinstance(qualification, Mapping) else {}
    )
    # These are per-Registry storage locators, not achieved coverage semantics.
    for field_name in (
        "coverage_artifact_id",
        "coverage_artifact_hash",
        "coverage_artifact_path",
        "observation_artifact_ids",
        "observation_artifact_hashes",
        "observation_artifact_paths",
    ):
        qualification_projection.pop(field_name, None)
    return hash_json(
        {
            "canonical_paper_key": binding.canonical_paper_key,
            "source_pdf_content_sha256": binding.source_pdf_content_sha256,
            "binding_semantics": projection,
            "visual_evidence_qualification": qualification_projection,
            "normalized_summary": payload,
        }
    )


def discover_automatic_stage1_reuse(
    *,
    base_output_dir: str | Path,
    current_workspace_dir: str | Path,
    current_job_id: str,
    paper_work_items: Sequence[Any],
    summary_payloads_from_file: Callable[[str | Path], Sequence[Mapping[str, Any]]],
    external_registry_resolver: Callable[[str], ArtifactRegistry | None] | None = None,
) -> AutomaticStage1ReusePlan:
    """Find exact-key/PDF-matched typed manifests in sibling workspaces.

    Discovery stays within the configured output root and one directory level.
    It never chooses among semantically different owners: affected paper keys
    are omitted from the returned candidate set and are regenerated through
    the existing Stage 1 path. Equivalent candidates use a stable Registry
    identity ordering; the normal Stage 1 evaluator still decides eligibility.
    """

    expected_hashes: dict[str, str] = {}
    rejected: list[dict[str, Any]] = []
    for item in paper_work_items:
        key = str(getattr(item, "canonical_paper_key", "") or "").strip()
        pdf_path = str(getattr(item, "source_pdf", "") or "").strip()
        if not key or key in expected_hashes:
            raise ValueError("automatic Stage 1 reuse requires unique nonblank paper keys")
        try:
            expected_hashes[key] = file_sha256(pdf_path)
        except (OSError, TypeError, ValueError):
            # Stage1AnalysisService owns the missing/invalid-PDF diagnostic.
            continue

    try:
        output_root = Path(base_output_dir).expanduser().resolve(strict=True)
        current_root = Path(current_workspace_dir).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return AutomaticStage1ReusePlan((), {}, ())
    if not output_root.is_dir():
        return AutomaticStage1ReusePlan((), {}, ())

    choices: dict[str, list[AutomaticStage1ReuseCandidate]] = {
        key: [] for key in expected_hashes
    }
    superseded_prior_v1_by_key: dict[str, set[tuple[str, str, str, str]]] = {}
    blocked_v2_keys: set[str] = set()
    try:
        entries = sorted(output_root.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return AutomaticStage1ReusePlan((), {}, ())

    for workspace_entry in entries:
        if workspace_entry.is_symlink() or not workspace_entry.is_dir():
            continue
        try:
            owner_root = workspace_entry.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if owner_root.parent != output_root or owner_root == current_root:
            continue
        registry_path = owner_root / "artifact_registry.json"
        if registry_path.is_symlink() or not registry_path.is_file():
            continue
        try:
            resolved_registry_path = registry_path.resolve(strict=True)
            if not _inside(resolved_registry_path, owner_root):
                continue
            registry_payload = json.loads(resolved_registry_path.read_text(encoding="utf-8"))
            owner_job_id = (
                str(registry_payload.get("job_id") or "")
                if isinstance(registry_payload, Mapping)
                else ""
            )
            if not owner_job_id or owner_job_id == str(current_job_id):
                continue
            owner = ArtifactRegistry(str(resolved_registry_path), owner_job_id)
        except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError):
            rejected.append(
                {
                    "reason": "automatic_reuse_candidate_registry_unreadable",
                    "workspace_name": workspace_entry.name,
                }
            )
            continue

        for record in owner.list_records():
            manifest: Mapping[str, Any] = {}
            key = ""
            relevant = False
            prior_identity_for_record: tuple[str, str, str, str] | None = None
            record_version = str(record.artifact_version or "")
            if (
                record.status != "ready"
                or record.artifact_type != "stage1_reusable_summary_manifest"
                or record_version not in {"v1", "v2"}
            ):
                continue
            registered_v2_keys = (
                _registered_v2_target_keys(
                    owner,
                    record,
                    external_registry_resolver,
                )
                if record_version == "v2"
                else set()
            )
            expected_registered_v2_keys = registered_v2_keys.intersection(expected_hashes)
            if len(expected_registered_v2_keys) == 1:
                key = next(iter(expected_registered_v2_keys))
            elif expected_registered_v2_keys:
                blocked_v2_keys.update(expected_registered_v2_keys)
            try:
                manifest_path = Path(record.path).expanduser().resolve(strict=True)
                if not _inside(manifest_path, owner_root):
                    raise ValueError("registered manifest path escapes its owner workspace")
                manifest_hash = file_sha256(manifest_path)
                if record_version == "v2" and (
                    not record.content_hash or manifest_hash != record.content_hash
                ):
                    blocked_v2_keys.update(expected_registered_v2_keys)
                    raise ValueError("registered manifest file hash mismatch")
                manifest = _manifest_body(manifest_path)
                payload_key = str(manifest.get("canonical_paper_key") or "").strip()
                if record_version == "v2" and expected_registered_v2_keys:
                    if (
                        len(expected_registered_v2_keys) != 1
                        or payload_key not in expected_registered_v2_keys
                    ):
                        blocked_v2_keys.update(expected_registered_v2_keys)
                        raise ValueError(
                            "V2 payload paper key disagrees with its registered correction proof"
                        )
                    key = next(iter(expected_registered_v2_keys))
                else:
                    key = payload_key
                binding_payload = manifest.get("binding")
                binding_payload = binding_payload if isinstance(binding_payload, Mapping) else {}
                source_hash = str(
                    manifest.get("source_pdf_content_sha256")
                    or binding_payload.get("source_pdf_content_sha256")
                    or ""
                ).strip()
                relevant = key in expected_hashes and (
                    source_hash == expected_hashes[key]
                    if record_version == "v1" or source_hash
                    else True
                )
                if record_version != "v2" and (
                    not record.content_hash or manifest_hash != record.content_hash
                ):
                    raise ValueError("registered manifest file hash mismatch")
                if record_version == "v2" and relevant:
                    authority_payload = manifest.get("owner_correction_authority")
                    authority_payload = authority_payload if isinstance(authority_payload, Mapping) else {}
                    prior_ref = authority_payload.get("origin_prior_manifest")
                    prior_identity = _manifest_reference_identity(prior_ref)
                    origin_registry = authority_payload.get("origin_registry")
                    origin_registry = (
                        origin_registry if isinstance(origin_registry, Mapping) else {}
                    )
                    origin_job_id = str(origin_registry.get("job_id") or "").strip()
                    if prior_identity is None or origin_job_id != prior_identity[0]:
                        blocked_v2_keys.add(key)
                    else:
                        prior_identity_for_record = prior_identity
                if (
                    manifest.get("artifact_type") != record.artifact_type
                    or manifest.get("artifact_version") != record_version
                    or str(manifest.get("job_id") or "") != owner_job_id
                    or str(manifest.get("stage_name") or "") != "stage1_analyze"
                ):
                    raise ValueError("manifest identity disagrees with its Registry owner")
                if not relevant:
                    if key in expected_hashes:
                        rejected.append(
                            {
                                "canonical_paper_key": key,
                                "owner_job_id": owner_job_id,
                                "manifest_artifact_id": record.artifact_id,
                                "manifest_artifact_hash": record.content_hash,
                                "reason": "automatic_reuse_source_pdf_hash_mismatch",
                            }
                        )
                    continue
                if not isinstance(binding_payload, Mapping) or not binding_payload:
                    raise TypeError("typed manifest binding is missing")
                owner.verify_ready_artifact_closure(
                    record,
                    external_registry_resolver=external_registry_resolver,
                )
                parsed = list(summary_payloads_from_file(manifest_path))
                if len(parsed) != 1 or not isinstance(parsed[0], Mapping):
                    raise ValueError("typed manifest did not produce exactly one summary")
                summary = dict(parsed[0])
                paper_info = summary.get("paper_info")
                reuse_meta = summary.get("stage1_reuse")
                if (
                    not isinstance(paper_info, Mapping)
                    or str(paper_info.get("canonical_paper_key") or "").strip() != key
                    or not isinstance(reuse_meta, Mapping)
                    or str(reuse_meta.get("authority_kind") or "") != "typed_manifest"
                    or Path(str(reuse_meta.get("typed_manifest_path") or "")).resolve() != manifest_path
                    or str(reuse_meta.get("typed_manifest_artifact_hash") or "") != manifest_hash
                ):
                    raise ValueError("parsed summary identity disagrees with the registered manifest")
                binding = Stage1ReusableSummaryBindingV1.from_mapping(
                    reuse_meta.get("binding")
                )
                typed_authority, reason = verify_stage1_typed_manifest_authority(
                    summary,
                    binding,
                    external_registry_resolver=external_registry_resolver,
                )
                if (
                    typed_authority is None
                    or typed_authority.manifest.job_id != owner_job_id
                    or Path(typed_authority.manifest_path).resolve() != manifest_path
                    or typed_authority.manifest_file_hash != manifest_hash
                    or typed_authority.manifest.canonical_paper_key != key
                ):
                    raise ValueError(f"typed manifest authority rejected: {reason}")

                superseded_prior_identity: tuple[str, str, str, str] | None = None
                validation_source_manifests: list[dict[str, str]] = [
                    {
                        "owner_job_id": owner_job_id,
                        "owner_registry_id": f"artifact-registry:{owner_job_id}",
                        "owner_registry_path": str(resolved_registry_path),
                        "manifest_artifact_id": record.artifact_id,
                        "manifest_artifact_hash": record.content_hash,
                        "manifest_artifact_version": record_version,
                        "manifest_path": str(manifest_path),
                    }
                ]
                if record_version == "v2":
                    if not isinstance(typed_authority, Stage1TypedManifestAuthorityV2):
                        raise TypeError("V2 manifest did not produce the verified V2 authority type")
                    authority_payload = typed_authority.manifest.owner_correction_authority
                    origin_registry_payload = authority_payload.get("origin_registry")
                    origin_registry_payload = (
                        origin_registry_payload if isinstance(origin_registry_payload, Mapping) else {}
                    )
                    prior_ref = authority_payload.get("origin_prior_manifest")
                    prior_ref = prior_ref if isinstance(prior_ref, Mapping) else {}
                    prior_identity = _manifest_reference_identity(prior_ref)
                    prior_path = Path(
                        typed_authority.owner_correction_prior_manifest_path
                    ).expanduser().resolve(strict=True)
                    if prior_identity is None or prior_identity[3] != str(prior_path):
                        raise ValueError("verified V2 authority has no exact prior V1 locator")
                    prior_job_id, prior_artifact_id, prior_hash, _prior_path = prior_identity
                    prior_registry = (
                        external_registry_resolver(prior_job_id)
                        if external_registry_resolver is not None
                        else None
                    )
                    if prior_registry is None:
                        raise ValueError("verified V2 origin Registry is unavailable for source binding")
                    prior_registry.reload()
                    prior_record = prior_registry.get(prior_artifact_id)
                    if (
                        prior_record is None
                        or prior_record.job_id != prior_job_id
                        or prior_record.status != "ready"
                        or prior_record.artifact_type != "stage1_reusable_summary_manifest"
                        or prior_record.artifact_version != "v1"
                        or prior_record.content_hash != prior_hash
                        or Path(prior_record.path).resolve() != prior_path
                    ):
                        raise ValueError("verified V2 origin prior V1 record identity changed")
                    prior_registry.verify_ready_artifact_closure(
                        prior_record,
                        external_registry_resolver=external_registry_resolver,
                    )
                    origin_registry_path = str(
                        origin_registry_payload.get("registry_path")
                        or prior_registry.registry_path
                    )
                    if Path(origin_registry_path).resolve() != Path(prior_registry.registry_path).resolve():
                        raise ValueError("verified V2 origin Registry path disagrees with its owner")
                    superseded_prior_identity = prior_identity
                    validation_source_manifests.append(
                        {
                            "owner_job_id": prior_job_id,
                            "owner_registry_id": f"artifact-registry:{prior_job_id}",
                            "owner_registry_path": str(Path(prior_registry.registry_path).resolve()),
                            "manifest_artifact_id": prior_artifact_id,
                            "manifest_artifact_hash": prior_hash,
                            "manifest_artifact_version": "v1",
                            "manifest_path": str(prior_path),
                        }
                    )
                    proposal_ref = authority_payload.get("proposal")
                    proposal_ref = proposal_ref if isinstance(proposal_ref, Mapping) else {}
                    proposal_record = owner.get(str(proposal_ref.get("artifact_id") or ""))
                    if (
                        proposal_record is None
                        or proposal_record.status != "quarantined"
                        or proposal_record.artifact_type != "stage1_summary_correction_proposal"
                        or proposal_record.artifact_version != "v1"
                        or proposal_record.job_id != owner_job_id
                        or proposal_record.content_hash != str(proposal_ref.get("content_hash") or "")
                        or file_sha256(proposal_record.path) != proposal_record.content_hash
                    ):
                        raise ValueError("verified V2 source-PDF proposal record identity changed")
                    proposal_payload = _manifest_body(Path(proposal_record.path))
                    normalized_proposal = proposal_payload.get("normalized_proposal")
                    normalized_proposal = (
                        normalized_proposal if isinstance(normalized_proposal, Mapping) else {}
                    )
                    proposal_pdf_hash = str(
                        normalized_proposal.get("source_pdf_sha256") or ""
                    ).strip()
                    if (
                        len(proposal_pdf_hash) != 64
                        or proposal_pdf_hash != expected_hashes[key]
                    ):
                        rejected.append(
                            {
                                "canonical_paper_key": key,
                                "owner_job_id": owner_job_id,
                                "manifest_artifact_id": record.artifact_id,
                                "manifest_artifact_hash": record.content_hash,
                                "reason": "automatic_reuse_source_pdf_hash_mismatch",
                            }
                        )
                        continue
                    if prior_identity_for_record != prior_identity:
                        raise ValueError("verified V2 exact prior identity differs from the source-bound reference")
                    superseded_prior_v1_by_key.setdefault(key, set()).add(prior_identity)

                candidate = AutomaticStage1ReuseCandidate(
                    canonical_paper_key=key,
                    summary=summary,
                    owner_job_id=owner_job_id,
                    owner_registry_path=str(resolved_registry_path),
                    owner_registry_id=f"artifact-registry:{owner_job_id}",
                    owner_registry_revision=int(owner.revision),
                    manifest_record_id=record.artifact_id,
                    manifest_record_hash=record.content_hash,
                    manifest_record_path=str(manifest_path),
                    manifest_file_hash=manifest_hash,
                    manifest_record_version=record_version,
                    validation_source_manifests=tuple(validation_source_manifests),
                    superseded_prior_v1_identity=superseded_prior_identity,
                    equivalence_hash=_equivalence_hash(summary, binding),
                    record=record,
                )
                choices[key].append(candidate)
            except (OSError, UnicodeError, json.JSONDecodeError, RegistryError, TypeError, ValueError, RuntimeError) as exc:
                if record_version == "v2" and relevant:
                    blocked_v2_keys.add(key)
                elif record_version == "v2":
                    blocked_v2_keys.update(expected_registered_v2_keys)
                rejected.append(
                    {
                        "canonical_paper_key": key,
                        "owner_job_id": owner_job_id,
                        "manifest_artifact_id": record.artifact_id,
                        "manifest_artifact_hash": record.content_hash,
                        "reason": "automatic_reuse_candidate_authority_rejected",
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )

    selected: dict[str, AutomaticStage1ReuseCandidate] = {}
    for key, candidates in choices.items():
        if key in blocked_v2_keys:
            rejected.append(
                {
                    "canonical_paper_key": key,
                    "reason": "automatic_reuse_unverified_v2_owner_quarantines_key",
                }
            )
            continue
        superseded = superseded_prior_v1_by_key.get(key, set())
        if superseded:
            retained: list[AutomaticStage1ReuseCandidate] = []
            for candidate in candidates:
                exact_identity = (
                    candidate.owner_job_id,
                    candidate.manifest_record_id,
                    candidate.manifest_record_hash,
                    str(Path(candidate.manifest_record_path).resolve()),
                )
                if (
                    candidate.manifest_record_version == "v1"
                    and exact_identity in superseded
                ):
                    rejected.append(
                        {
                            "canonical_paper_key": key,
                            "owner_job_id": candidate.owner_job_id,
                            "manifest_artifact_id": candidate.manifest_record_id,
                            "manifest_artifact_hash": candidate.manifest_record_hash,
                            "reason": "automatic_reuse_v1_superseded_by_exact_v2_correction",
                        }
                    )
                    continue
                retained.append(candidate)
            candidates = retained
        if not candidates:
            continue
        equivalence_hashes = {candidate.equivalence_hash for candidate in candidates}
        if len(equivalence_hashes) > 1:
            rejected.append(
                {
                    "canonical_paper_key": key,
                    "reason": "automatic_reuse_conflicting_non_equivalent_owners",
                    "candidates": [
                        {
                            "owner_job_id": candidate.owner_job_id,
                            "manifest_artifact_id": candidate.manifest_record_id,
                            "manifest_artifact_hash": candidate.manifest_record_hash,
                        }
                        for candidate in sorted(
                            candidates,
                            key=lambda value: (
                                value.owner_job_id,
                                value.manifest_record_id,
                                value.manifest_record_hash,
                            ),
                        )
                    ],
                }
            )
            continue
        selected[key] = min(
            candidates,
            key=lambda value: (
                value.owner_job_id,
                value.manifest_record_id,
                value.manifest_record_hash,
            ),
        )

    ordered_candidates = {
        key: selected[key] for key in expected_hashes if key in selected
    }
    summaries = tuple(dict(candidate.summary) for candidate in ordered_candidates.values())
    return AutomaticStage1ReusePlan(
        summaries=summaries,
        candidates_by_key=ordered_candidates,
        rejected_candidates=tuple(rejected),
    )

"""Opt-in original response capture for explicit successor provider contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runtime.provider_response_snapshot import ProviderResponseSnapshotStore
from runtime.source_revision import (
    PROVIDER_SEMANTIC_CONTRACT_V2,
    SourceRevisionError,
    provider_semantic_contract_version,
    validate_source_revision_metadata,
)
from services.artifact_registry import ArtifactRegistry
from services.job_workspace import JobWorkspace


def build_source_revision_response_sink(
    metadata: Mapping[str, Any],
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    publication_context: Any,
) -> ProviderResponseSnapshotStore | None:
    """Install capture only for an explicitly selected successor contract.

    A snapshot grants no source, receipt, reuse or provider admission authority.
    Legacy metadata stays untouched and creates no store or filesystem state.
    """

    if not isinstance(metadata, Mapping):
        raise SourceRevisionError("source revision response metadata must be an object")
    validate_source_revision_metadata(metadata)
    if provider_semantic_contract_version(metadata) != PROVIDER_SEMANTIC_CONTRACT_V2:
        return None
    return ProviderResponseSnapshotStore(workspace, registry, publication_context)

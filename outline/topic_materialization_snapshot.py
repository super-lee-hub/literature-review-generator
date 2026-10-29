"""Operation-scoped serialized snapshots for local topic planning."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from collections.abc import MutableMapping, MutableSequence, MutableSet
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from outline.v3_models import (
    EvidenceClaim,
    InterpretationDependency,
    OutlineEvidenceView,
    OutlineEvidenceViews,
    PaperContentLayers,
    PaperEvidenceDossier,
    PaperIndexCard,
    ResearchUnit,
    SourceFieldLedgerEntry,
    compute_v3_hash,
)

class TopicMaterializationSourceChanged(RuntimeError):
    """Raised when planner inputs change during one materialization operation."""


@dataclass(frozen=True)
class TopicMaterializationSnapshot:
    """The two models the topic planner should use within the snapshot scope."""

    evidence_model: Any
    content_layers_model: Any
    cached: bool


class _SnapshotLease:
    def __init__(self) -> None:
        self.active = True

    def require_active(self) -> None:
        if not self.active:
            raise RuntimeError("topic materialization snapshot is closed")


class _SnapshotReadViewBehavior:
    """Shared typed read-view behavior for standard v3 model subclasses."""

    def __init__(
        self,
        source: Any,
        payload: Mapping[str, Any],
        *,
        canonical: Mapping[str, Any] | None = None,
        hashes: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Callable[[], Any]] | None = None,
        lease: _SnapshotLease,
    ) -> None:
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_payload", deepcopy(dict(payload)))
        object.__setattr__(self, "_canonical", deepcopy(dict(canonical)) if canonical is not None else None)
        object.__setattr__(self, "_hashes", dict(hashes or {}))
        object.__setattr__(self, "_overrides", dict(overrides or {}))
        object.__setattr__(self, "_lease", lease)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_") or (name.startswith("__") and name.endswith("__")):
            return object.__getattribute__(self, name)
        lease = object.__getattribute__(self, "_lease")
        lease.require_active()
        if name == "to_dict":
            return object.__getattribute__(self, "_snapshot_to_dict")
        if name == "canonical_payload":
            canonical = object.__getattribute__(self, "_canonical")
            if canonical is not None:
                return object.__getattribute__(self, "_snapshot_canonical_payload")
            source = object.__getattribute__(self, "_source")
            return getattr(source, name)
        hashes = object.__getattribute__(self, "_hashes")
        if name in hashes:
            return deepcopy(hashes[name])
        overrides = object.__getattribute__(self, "_overrides")
        override = overrides.get(name)
        if override is not None:
            return override()
        source = object.__getattribute__(self, "_source")
        value = getattr(source, name)
        if isinstance(value, (MutableMapping, MutableSequence, MutableSet, bytearray, tuple)):
            return deepcopy(value)
        return value

    def _snapshot_to_dict(self) -> dict[str, Any]:
        object.__getattribute__(self, "_lease").require_active()
        return deepcopy(object.__getattribute__(self, "_payload"))

    def _snapshot_canonical_payload(self) -> dict[str, Any]:
        object.__getattribute__(self, "_lease").require_active()
        canonical = object.__getattribute__(self, "_canonical")
        if canonical is None:
            raise AttributeError("canonical_payload")
        return deepcopy(dict(canonical))


class _SnapshotOutlineEvidenceView(_SnapshotReadViewBehavior, OutlineEvidenceView):
    pass


class _SnapshotOutlineEvidenceViews(_SnapshotReadViewBehavior, OutlineEvidenceViews):
    pass


class _SnapshotPaperEvidenceDossier(_SnapshotReadViewBehavior, PaperEvidenceDossier):
    pass


class _SnapshotPaperIndexCard(_SnapshotReadViewBehavior, PaperIndexCard):
    pass


class _SnapshotPaperContentLayers(_SnapshotReadViewBehavior, PaperContentLayers):
    pass


def _identity(model_type: type[Any], payload: Mapping[str, Any]) -> str:
    return compute_v3_hash({
        "model_type": f"{model_type.__module__}.{model_type.__qualname__}",
        "serialized": payload,
    })


def _evidence_serialization(model: OutlineEvidenceViews) -> tuple[dict[str, Any], dict[str, Any], str]:
    canonical = deepcopy(model.canonical_payload())
    content_hash = compute_v3_hash(canonical)
    payload = deepcopy(canonical)
    payload.update({
        "created_from_job_id": model.created_from_job_id,
        "shard_id": model.shard_id,
        "shard_count": model.shard_count,
        "status": model.status,
        "content_hash": content_hash,
    })
    return canonical, payload, _identity(type(model), payload)


def _content_layers_serialization(model: PaperContentLayers) -> tuple[dict[str, Any], dict[str, Any], str]:
    canonical = deepcopy(model.canonical_payload())
    content_hash = compute_v3_hash(canonical)
    payload = deepcopy(canonical)
    payload.update({"status": model.status, "content_hash": content_hash})
    return canonical, payload, _identity(type(model), payload)


def _entry_is_standard(entry: Any) -> bool:
    return type(entry) is SourceFieldLedgerEntry


def _dossier_is_standard(dossier: Any) -> bool:
    return (
        type(dossier) is PaperEvidenceDossier
        and all(type(unit) is ResearchUnit for unit in dossier.research_units)
        and all(type(claim) is EvidenceClaim for claim in dossier.claims)
        and all(type(claim) is EvidenceClaim
                for unit in dossier.research_units for claim in unit.claims)
        and all(type(dependency) is InterpretationDependency
                for unit in dossier.research_units for dependency in unit.interpretation_dependencies)
        and all(_entry_is_standard(entry) for entry in dossier.source_field_ledger)
    )


def _standard_graph(evidence_model: Any, content_layers_model: Any) -> bool:
    return (
        type(evidence_model) is OutlineEvidenceViews
        and type(content_layers_model) is PaperContentLayers
        and all(type(view) is OutlineEvidenceView for view in evidence_model.views)
        and all(all(_entry_is_standard(entry) for entry in view.source_field_ledger)
                for view in evidence_model.views)
        and all(type(card) is PaperIndexCard for card in content_layers_model.index_cards)
        and all(_dossier_is_standard(dossier) for dossier in content_layers_model.dossiers)
    )


def _is_standard_graph(evidence_model: Any, content_layers_model: Any) -> bool:
    try:
        return _standard_graph(evidence_model, content_layers_model)
    except Exception:
        return False


def _view_snapshot(
    view: OutlineEvidenceView,
    payload: Mapping[str, Any],
    lease: _SnapshotLease,
) -> _SnapshotOutlineEvidenceView:
    detached = deepcopy(dict(payload))
    return _SnapshotOutlineEvidenceView(
        view,
        detached,
        hashes={"view_hash": compute_v3_hash(detached)},
        lease=lease,
    )


def _dossier_snapshot(
    dossier: PaperEvidenceDossier,
    payload: Mapping[str, Any],
    lease: _SnapshotLease,
) -> _SnapshotPaperEvidenceDossier:
    detached = deepcopy(dict(payload))
    canonical = deepcopy(detached)
    evidence_ids = canonical.pop("evidence_ids")
    content_hash = canonical.pop("content_hash")
    if compute_v3_hash(canonical) != content_hash:
        raise ValueError("serialized dossier hash does not match its canonical payload")
    return _SnapshotPaperEvidenceDossier(
        dossier,
        detached,
        canonical=canonical,
        hashes={"content_hash": content_hash, "evidence_ids": evidence_ids},
        lease=lease,
    )


def _card_snapshot(card: PaperIndexCard, payload: Mapping[str, Any], lease: _SnapshotLease) -> _SnapshotPaperIndexCard:
    return _SnapshotPaperIndexCard(card, payload, lease=lease)


def _snapshot_evidence_model(
    model: OutlineEvidenceViews,
    lease: _SnapshotLease,
) -> tuple[_SnapshotOutlineEvidenceViews, str]:
    canonical, payload, identity = _evidence_serialization(model)
    source_views = sorted(model.views, key=lambda item: item.paper_key)
    view_payloads = canonical.get("views")
    if not isinstance(view_payloads, list) or len(source_views) != len(view_payloads):
        raise ValueError("evidence view serialization does not match its source views")
    sorted_snapshots = [
        _view_snapshot(view, view_payload, lease)
        for view, view_payload in zip(source_views, view_payloads, strict=True)
    ]
    by_source_id = {id(view): snapshot for view, snapshot in zip(source_views, sorted_snapshots, strict=True)}
    views = [by_source_id[id(view)] for view in model.views]
    hashes = {"content_hash": payload["content_hash"], "artifact_hash": payload["content_hash"]}
    overrides = {
        "views": lambda: list(views),
        "evidence_views": lambda: list(views),
        "status": lambda: payload["status"],
    }
    return _SnapshotOutlineEvidenceViews(
        model, payload, canonical=canonical, hashes=hashes, overrides=overrides, lease=lease,
    ), identity


def _snapshot_content_layers_model(
    model: PaperContentLayers,
    lease: _SnapshotLease,
) -> tuple[_SnapshotPaperContentLayers, str]:
    canonical, payload, identity = _content_layers_serialization(model)
    source_dossiers = sorted(model.dossiers, key=lambda item: item.paper_id)
    dossier_payloads = canonical.get("dossiers")
    source_cards = sorted(model.index_cards, key=lambda item: item.paper_id)
    card_payloads = canonical.get("index_cards")
    if (not isinstance(dossier_payloads, list) or len(source_dossiers) != len(dossier_payloads)
            or not isinstance(card_payloads, list) or len(source_cards) != len(card_payloads)):
        raise ValueError("content-layer serialization does not match its source items")
    sorted_dossiers = [
        _dossier_snapshot(dossier, dossier_payload, lease)
        for dossier, dossier_payload in zip(source_dossiers, dossier_payloads, strict=True)
    ]
    dossier_by_source_id = {
        id(dossier): snapshot
        for dossier, snapshot in zip(source_dossiers, sorted_dossiers, strict=True)
    }
    dossiers = [dossier_by_source_id[id(dossier)] for dossier in model.dossiers]
    sorted_cards = [
        _card_snapshot(card, card_payload, lease)
        for card, card_payload in zip(source_cards, card_payloads, strict=True)
    ]
    card_by_source_id = {
        id(card): snapshot for card, snapshot in zip(source_cards, sorted_cards, strict=True)
    }
    cards = [card_by_source_id[id(card)] for card in model.index_cards]
    hashes = {"content_hash": payload["content_hash"]}
    overrides = {
        "dossiers": lambda: list(dossiers),
        "index_cards": lambda: list(cards),
        "dossier_by_paper": lambda: {str(item.paper_id): item for item in dossiers},
        "status": lambda: payload["status"],
    }
    return _SnapshotPaperContentLayers(
        model, payload, canonical=canonical, hashes=hashes, overrides=overrides, lease=lease,
    ), identity


def _fallback_identity(model: Any) -> str | None:
    serializer = getattr(model, "to_dict", None)
    if not callable(serializer):
        return None
    try:
        serialized = serializer()
        extras: dict[str, Any] = {}
        for name in ("content_hash", "artifact_hash", "view_hash"):
            try:
                extras[name] = getattr(model, name)
            except AttributeError:
                continue
        return _identity(type(model), {"to_dict": serialized, "derived_hashes": extras})
    except Exception:
        return None


def _verify_fallback_identity(model: Any, expected: str | None, label: str) -> None:
    if expected is not None and _fallback_identity(model) != expected:
        raise TopicMaterializationSourceChanged(f"{label} changed during topic materialization")


@contextmanager
def _passthrough_snapshot(
    evidence_model: Any,
    content_layers_model: Any,
) -> Iterator[TopicMaterializationSnapshot]:
    expected_evidence = _fallback_identity(evidence_model)
    expected_layers = _fallback_identity(content_layers_model)
    snapshot = TopicMaterializationSnapshot(evidence_model, content_layers_model, cached=False)
    try:
        yield snapshot
    except BaseException as original_error:
        try:
            _verify_fallback_identity(evidence_model, expected_evidence, "evidence model")
            _verify_fallback_identity(content_layers_model, expected_layers, "content layers")
        except TopicMaterializationSourceChanged as drift:
            raise drift from original_error
        raise
    else:
        _verify_fallback_identity(evidence_model, expected_evidence, "evidence model")
        _verify_fallback_identity(content_layers_model, expected_layers, "content layers")


@contextmanager
def snapshot_topic_materialization(
    evidence_model: Any,
    content_layers_model: Any,
) -> Iterator[TopicMaterializationSnapshot]:
    """Cache standard source serialization for one planning operation only.

    Nonstandard/custom model graphs pass through untouched and uncached. Standard
    graphs are copied once; their read views return detached cached payloads while
    all other attributes come from the private copy. The original serialized
    identities are checked before the context exits successfully.
    """

    if not _is_standard_graph(evidence_model, content_layers_model):
        with _passthrough_snapshot(evidence_model, content_layers_model) as snapshot:
            yield snapshot
        return

    try:
        entry_evidence_identity = _evidence_serialization(evidence_model)[2]
        entry_layers_identity = _content_layers_serialization(content_layers_model)[2]
    except Exception:
        with _passthrough_snapshot(evidence_model, content_layers_model) as snapshot:
            yield snapshot
        return

    try:
        private_evidence, private_layers = deepcopy((evidence_model, content_layers_model))
    except Exception as copy_error:
        try:
            current_evidence_identity = _evidence_serialization(evidence_model)[2]
            current_layers_identity = _content_layers_serialization(content_layers_model)[2]
        except Exception as identity_error:
            raise TopicMaterializationSourceChanged(
                "topic materialization source changed or became unreadable during private copy"
            ) from identity_error
        if (current_evidence_identity != entry_evidence_identity
                or current_layers_identity != entry_layers_identity):
            raise TopicMaterializationSourceChanged(
                "topic materialization source changed during private copy"
            ) from copy_error
        with _passthrough_snapshot(evidence_model, content_layers_model) as snapshot:
            yield snapshot
        return
    if not _is_standard_graph(private_evidence, private_layers):
        raise TopicMaterializationSourceChanged(
            "private model graph changed during topic materialization copy"
        )

    lease = _SnapshotLease()
    try:
        evidence_snapshot, private_evidence_identity = _snapshot_evidence_model(private_evidence, lease)
        layers_snapshot, private_layers_identity = _snapshot_content_layers_model(private_layers, lease)
    except Exception as snapshot_error:
        lease.active = False
        try:
            current_evidence_identity = _evidence_serialization(evidence_model)[2]
            current_layers_identity = _content_layers_serialization(content_layers_model)[2]
        except Exception as identity_error:
            raise TopicMaterializationSourceChanged(
                "topic materialization source changed or became unreadable during snapshot creation"
            ) from identity_error
        if (current_evidence_identity != entry_evidence_identity
                or current_layers_identity != entry_layers_identity):
            raise TopicMaterializationSourceChanged(
                "topic materialization source changed during snapshot creation"
            ) from snapshot_error
        # Preserve the uncached path if a standard graph cannot be projected.
        with _passthrough_snapshot(evidence_model, content_layers_model) as snapshot:
            yield snapshot
        return

    if (private_evidence_identity != entry_evidence_identity
            or private_layers_identity != entry_layers_identity):
        lease.active = False
        raise TopicMaterializationSourceChanged(
            "private model snapshot differs from the serialized identity captured at entry"
        )

    expected_evidence = entry_evidence_identity
    expected_layers = entry_layers_identity

    snapshot = TopicMaterializationSnapshot(evidence_snapshot, layers_snapshot, cached=True)

    def verify_sources() -> None:
        try:
            current_evidence = _evidence_serialization(evidence_model)[2]
            current_layers = _content_layers_serialization(content_layers_model)[2]
        except Exception as exc:
            raise TopicMaterializationSourceChanged(
                "topic materialization source could not be serialized at scope exit"
            ) from exc
        if current_evidence != expected_evidence:
            raise TopicMaterializationSourceChanged("evidence model changed during topic materialization")
        if current_layers != expected_layers:
            raise TopicMaterializationSourceChanged("content layers changed during topic materialization")

    try:
        try:
            yield snapshot
        except BaseException as original_error:
            try:
                verify_sources()
            except TopicMaterializationSourceChanged as drift:
                raise drift from original_error
            raise
        else:
            verify_sources()
    finally:
        lease.active = False

"""Append-only, additional-only authority for an adopted job's downstream work.

The expired parent budget is immutable history. A continuation has its own V5
ledger and finite clock, with total exposure bounded by the parent's remainder.
Only the public prepare/load functions mint the in-process verification token.
"""

from __future__ import annotations

import configparser
import hashlib
import ipaddress
import json
import math
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from runtime.attempt_store import AttemptExecutionLease
from runtime.provider_run_envelope import ProviderRunEnvelopeV1
from runtime.provider_runtime import (
    ProviderAggregateBudgetV2,
    ProviderBudgetController,
    hash_json,
)
from services.artifact_registry import (
    ArtifactDependencyRefV2,
    ArtifactRecord,
    ArtifactRegistry,
    file_sha256,
)
from services.job_workspace import JobWorkspace, publish_json_artifact
from services.queue_service import LocalPublicationContext

GRANT_TYPE = "downstream_continuation_grant"
POINTER_TYPE = "downstream_continuation_pointer"
CURRENT_ID = "downstream-continuation:current"
ALLOW_PROVIDER_STAGES = ("stage3_review", "stage4_validate")
_TOKEN = object()
_CURRENT: ContextVar[VerifiedDownstreamContinuation | None] = ContextVar(
    "verified_downstream_continuation", default=None
)
_ROOTS = {
    "runtime_job_spec": ("runtime_job_spec", "v1"),
    "source_inventory": ("source_inventory", "v1"),
    "provider_run_envelope": ("provider_run_envelope", "v1"),
    "outline-v3:adoption:current": ("outline_adoption_pointer", "v1"),
    "outline-v3:final_outline": ("final_outline", "v3"),
    "outline-v3:section_evidence_packets": ("section_evidence_packet_set", "v3"),
}
_COUNTERS = ("calls_used", "output_tokens_used", "retry_attempts_used")


class DownstreamContinuationError(ValueError):
    """A downstream grant is missing, expired, stale or outside its authority."""


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DownstreamContinuationError(f"JSON object required: {path}")
    return value


def _count(value: Any, label: str) -> int:
    if type(value) is not int or value < 0:
        raise DownstreamContinuationError(f"invalid nonnegative integer: {label}")
    return value


def _time(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise DownstreamContinuationError(f"invalid finite timestamp: {label}")
    return float(value)


def _ready(registry: ArtifactRegistry, artifact_id: str) -> ArtifactRecord:
    record = registry.get(artifact_id)
    if record is None or record.job_id != registry.job_id or record.status != "ready":
        raise DownstreamContinuationError(
            f"required local READY record missing: {artifact_id}"
        )
    if (
        not Path(record.path).is_file()
        or file_sha256(record.path) != record.content_hash
    ):
        raise DownstreamContinuationError(f"artifact bytes changed: {artifact_id}")
    return record


def _ref(record: ArtifactRecord) -> dict[str, str]:
    return ArtifactDependencyRefV2.from_record(record).to_dict()


def _unwrap(value: Mapping[str, Any]) -> Mapping[str, Any]:
    current = value
    for _ in range(3):
        if not isinstance(current.get("payload"), Mapping):
            break
        current = current["payload"]
    return current


def _workspace_check(workspace: JobWorkspace, registry: ArtifactRegistry) -> None:
    if (
        workspace.job_id != registry.job_id
        or Path(workspace.paths.registry_path).resolve()
        != Path(registry.registry_path).resolve()
    ):
        raise DownstreamContinuationError(
            "continuation Registry belongs to another workspace/job"
        )


def _parent(
    workspace: JobWorkspace,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    path = Path(workspace.log_path("acceptance_budget_state_v1.json"))
    identity_path = path.with_name(path.name + ".identity.json")
    state, identity = _object(path), _object(identity_path)
    if state.get("schema_version") != "provider-aggregate-budget-v5":
        raise DownstreamContinuationError(
            "continuation parent must use immutable strict V5 accounting"
        )
    budget = ProviderAggregateBudgetV2.from_mapping(state.get("budget") or {})
    for name in _COUNTERS:
        _count(state.get(name), name)
    if (
        any(
            _count(state.get(name), name)
            for name in (
                "calls_reserved",
                "output_tokens_reserved",
                "retry_attempts_reserved",
            )
        )
        or state.get("reservations") != []
    ):
        raise DownstreamContinuationError("parent has unresolved provider reservations")
    start = _time(state.get("first_started_epoch"), "parent first start")
    deadline = _time(state.get("absolute_deadline_epoch"), "parent deadline")
    if not math.isclose(
        start + budget.max_wall_seconds, deadline, rel_tol=0, abs_tol=1e-6
    ):
        raise DownstreamContinuationError(
            "parent deadline does not match its original budget"
        )
    if deadline > time.time():
        raise DownstreamContinuationError(
            "parent budget must be expired before additional authorization"
        )
    run_id = f"run:{workspace.job_id}"
    if (
        state.get("acceptance_run_id") != run_id
        or identity.get("schema_version") != "provider-aggregate-budget-identity-v1"
        or identity.get("acceptance_run_id") != run_id
        or identity.get("state_path") != str(path.resolve())
        or not isinstance(identity.get("created_at"), str)
        or not identity["created_at"]
    ):
        raise DownstreamContinuationError(
            "parent budget/identity belongs to another run"
        )
    remaining = (
        budget.max_provider_calls_total - state["calls_used"],
        budget.max_output_tokens_total - state["output_tokens_used"],
        budget.max_retry_attempts_total - state["retry_attempts_used"],
    )
    if min(remaining) < 0:
        raise DownstreamContinuationError("parent usage exceeds its approved totals")
    return (
        state,
        identity,
        {
            "state_path": str(path),
            "state_sha256": file_sha256(path),
            "identity_path": str(identity_path),
            "identity_sha256": file_sha256(identity_path),
        },
    )


def _scope(
    registry: ArtifactRegistry, spec_payload: Mapping[str, Any] | None
) -> tuple[list[ArtifactRecord], dict[str, Any], ProviderRunEnvelopeV1, dict[str, str]]:
    roots = []
    for artifact_id, identity in _ROOTS.items():
        record = _ready(registry, artifact_id)
        if (record.artifact_type, record.artifact_version) != identity:
            raise DownstreamContinuationError(
                f"scope type/version changed: {artifact_id}"
            )
        roots.append(record)
    by_id = {r.artifact_id: r for r in roots}
    spec = _object(Path(by_id["runtime_job_spec"].path))
    if spec.get("job_id") != registry.job_id or (
        spec_payload is not None and _json(spec_payload) != _json(spec)
    ):
        raise DownstreamContinuationError(
            "original runtime spec changed or belongs to another job"
        )
    envelope = ProviderRunEnvelopeV1.from_dict(
        _unwrap(_object(Path(by_id["provider_run_envelope"].path)))
    )
    if (
        envelope.job_id != registry.job_id
        or envelope.run_id != f"run:{registry.job_id}"
    ):
        raise DownstreamContinuationError(
            "original provider envelope belongs to another run"
        )
    if envelope.scope_hashes["runtime_spec_sha256"] != hash_json(spec):
        raise DownstreamContinuationError("original scope does not bind runtime spec")
    original_budget = ProviderAggregateBudgetV2.from_mapping(
        spec.get("metadata", {}).get("provider_budget") or {}
    )
    if original_budget != envelope.provider_budget:
        raise DownstreamContinuationError("original spec and envelope budgets differ")
    binding = by_id["runtime_job_spec"].metadata.get("config_snapshot_binding") or {}
    config_path = Path(str(spec.get("config") or "")).resolve()
    if (
        binding.get("config_source_id") != str(config_path)
        or file_sha256(config_path) != binding.get("config_source_sha256")
        or binding.get("effective_config_sha256")
        != envelope.scope_hashes["effective_config_sha256"]
    ):
        raise DownstreamContinuationError(
            "original effective configuration/source bytes changed"
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(config_path, encoding="utf-8")
    from runtime.provider_routes import build_reachable_provider_route_plan

    routes = build_reachable_provider_route_plan(
        {name: dict(parser[name]) for name in parser.sections()},
        action="generate_review",
        requested_stages=("review", "validate"),
        free_mode_enabled=bool(
            spec.get("free_mode_profile")
            or spec.get("free_mode_idea")
            or spec.get("metadata", {}).get("free_mode_input")
        ),
    )
    if not routes.routes:
        raise DownstreamContinuationError("downstream routes are missing")
    for route in routes.routes:
        if not route.enabled:
            continue
        if not route.resolved:
            raise DownstreamContinuationError(
                "reachable downstream route is unresolved"
            )
        host = (
            urlsplit(str(parser[route.section_name].get("api_base") or "")).hostname
            or ""
        )
        try:
            loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise DownstreamContinuationError(
                "downstream continuation permits loopback provider hosts only"
            )
    pointer = _object(Path(by_id["outline-v3:adoption:current"].path))
    adopted = _ready(registry, str(pointer.get("current_adoption_artifact_id") or ""))
    if (adopted.artifact_type, adopted.artifact_version) != (
        "adopted_outline",
        "v3",
    ) or adopted.content_hash != pointer.get("current_adoption_hash"):
        raise DownstreamContinuationError(
            "current adoption pointer has changed authority"
        )
    payload = _unwrap(_object(Path(adopted.path)))
    if (
        payload.get("final_outline_hash")
        != by_id["outline-v3:final_outline"].content_hash
    ):
        raise DownstreamContinuationError(
            "adoption does not bind current final outline"
        )
    if (
        ArtifactDependencyRefV2.from_record(adopted)
        not in by_id["outline-v3:adoption:current"].depends_on
    ):
        raise DownstreamContinuationError(
            "current adoption pointer lost its typed dependency"
        )
    roots.append(adopted)
    return (
        roots,
        spec,
        envelope,
        {"config_path": str(config_path), "config_sha256": file_sha256(config_path)},
    )


@dataclass(frozen=True)
class VerifiedDownstreamContinuation:
    job_id: str
    budget: ProviderAggregateBudgetV2
    state_path: Path
    run_id: str
    artifact_record: ArtifactRecord
    original_spec_budget_hash: str
    original_scope_hashes: Mapping[str, str]
    carried_usage: Mapping[str, int]
    first_started_epoch: float
    absolute_deadline_epoch: float
    allow_provider_stages: tuple[str, ...] = ALLOW_PROVIDER_STAGES
    _verification_token: object = field(default=None, repr=False, compare=False)
    _verified_fields_sha256: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._verification_token is not _TOKEN
            or self.allow_provider_stages != ALLOW_PROVIDER_STAGES
        ):
            raise DownstreamContinuationError(
                "continuation requires verified local grant authority"
            )
        if self._verified_fields_sha256 != _verified_fields(self):
            raise DownstreamContinuationError(
                "verified continuation fields cannot be replaced"
            )
        object.__setattr__(
            self,
            "original_scope_hashes",
            MappingProxyType(dict(self.original_scope_hashes)),
        )
        object.__setattr__(
            self, "carried_usage", MappingProxyType(dict(self.carried_usage))
        )

    def overall_usage(self, additional: Mapping[str, Any]) -> dict[str, int]:
        return {
            name: self.carried_usage[name] + _count(additional.get(name), name)
            for name in _COUNTERS
        }


def _verified_fields(value: Any) -> str:
    return _sha(
        {
            "job_id": value.job_id,
            "budget": value.budget.to_dict(),
            "state_path": str(value.state_path),
            "run_id": value.run_id,
            "artifact_record": asdict(value.artifact_record),
            "original_spec_budget_hash": value.original_spec_budget_hash,
            "original_scope_hashes": dict(value.original_scope_hashes),
            "carried_usage": dict(value.carried_usage),
            "first_started_epoch": value.first_started_epoch,
            "absolute_deadline_epoch": value.absolute_deadline_epoch,
            "allow_provider_stages": list(value.allow_provider_stages),
        }
    )


def validate_continuation_artifact(record: ArtifactRecord, path: Path) -> None:
    payload = _object(path)
    if (
        record.artifact_version != "v1"
        or record.artifact_type not in {GRANT_TYPE, POINTER_TYPE}
        or record.artifact_role != record.artifact_type
        or record.producer != "runtime.downstream_continuation"
        or payload.get("job_id") != record.job_id
    ):
        raise DownstreamContinuationError(
            "continuation artifact type/version/job is invalid"
        )
    expected = f"{record.artifact_type}/v1"
    if payload.get("schema_version") != expected:
        raise DownstreamContinuationError("continuation artifact schema is invalid")
    if record.artifact_type == GRANT_TYPE:
        if tuple(payload.get("allow_provider_stages") or ()) != ALLOW_PROVIDER_STAGES:
            raise DownstreamContinuationError(
                "continuation cannot authorize upstream stages"
            )
        body = {k: v for k, v in payload.items() if k != "grant_sha256"}
        if payload.get("grant_sha256") != _sha(body):
            raise DownstreamContinuationError("continuation grant hash changed")
        if payload.get("accounting_semantics") != "additional_only_v5" or any(
            not isinstance(payload.get(name), str) or not payload[name].strip()
            for name in ("actor", "reason", "authorization_reference")
        ):
            raise DownstreamContinuationError(
                "continuation grant lacks explicit additional-only authorization"
            )


def _load_grant(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    record: ArtifactRecord,
    spec_payload: Mapping[str, Any] | None,
    expected_scope_hashes: Mapping[str, str] | None,
    *,
    head: ArtifactRecord | None = None,
) -> VerifiedDownstreamContinuation:
    initial_revision = registry.revision
    validate_continuation_artifact(record, Path(record.path))
    payload = _object(Path(record.path))
    roots, spec, envelope, config = _scope(registry, spec_payload)
    current_pins = {
        r.artifact_id: {"ref": _ref(r), "record_sha256": _sha(asdict(r))} for r in roots
    }
    if (
        _json(payload.get("scope_records")) != _json(current_pins)
        or payload.get("config") != config
    ):
        raise DownstreamContinuationError(
            "continuation source/adoption/spec/config records changed"
        )
    if dict(envelope.scope_hashes) != payload.get("original_scope_hashes") or (
        expected_scope_hashes is not None
        and dict(expected_scope_hashes) != dict(envelope.scope_hashes)
    ):
        raise DownstreamContinuationError(
            "continuation original run scope hashes changed"
        )
    if len(record.depends_on) != len(current_pins) or {
        _json(ref.to_dict()) for ref in record.depends_on
    } != {_json(pin["ref"]) for pin in current_pins.values()}:
        raise DownstreamContinuationError(
            "continuation grant lost typed scope dependencies"
        )
    old, _identity, old_pin = _parent(workspace)
    if payload.get("parent_ledger") != old_pin or payload.get("carried_usage") != {
        name: old[name] for name in _COUNTERS
    }:
        raise DownstreamContinuationError("immutable carried budget history changed")
    budget = ProviderAggregateBudgetV2.from_mapping(payload["budget"])
    original = ProviderAggregateBudgetV2.from_mapping(
        spec.get("metadata", {}).get("provider_budget") or {}
    )
    if (
        original != ProviderAggregateBudgetV2.from_mapping(old["budget"])
        or original != envelope.provider_budget
    ):
        raise DownstreamContinuationError("parent/spec/envelope budget limits differ")
    for cap, usage in (
        ("max_provider_calls_total", "calls_used"),
        ("max_output_tokens_total", "output_tokens_used"),
        ("max_retry_attempts_total", "retry_attempts_used"),
    ):
        if getattr(budget, cap) > getattr(original, cap) - old[usage]:
            raise DownstreamContinuationError(
                "continuation exceeds remaining parent allowance"
            )
    expected_grant_id = _sha({"job_id": workspace.job_id, "parent_ledger": old_pin})
    if (
        payload.get("grant_id") != expected_grant_id
        or record.artifact_id != f"downstream-continuation:{expected_grant_id}"
        or payload.get("run_id")
        != f"continuation:{workspace.job_id}:{expected_grant_id}"
    ):
        raise DownstreamContinuationError("continuation parent identity was forked")
    if payload.get("original_spec_budget_hash") != hash_json(original.to_dict()):
        raise DownstreamContinuationError("original spec budget hash changed")
    state_path = Path(
        workspace.log_path(f"downstream_continuations/{payload['grant_id']}.json")
    )
    if str(state_path) != payload.get("state_path"):
        raise DownstreamContinuationError("continuation state path is not job-owned")
    state, identity = (
        _object(state_path),
        _object(state_path.with_name(state_path.name + ".identity.json")),
    )
    if (
        state.get("schema_version") != "provider-aggregate-budget-v5"
        or state.get("acceptance_run_id") != payload["run_id"]
        or _json(state.get("budget")) != _json(budget.to_dict())
        or identity.get("schema_version") != "provider-aggregate-budget-identity-v1"
        or identity.get("acceptance_run_id") != payload["run_id"]
        or identity.get("state_path") != str(state_path)
        or file_sha256(state_path.with_name(state_path.name + ".identity.json"))
        != payload.get("state_identity_sha256")
    ):
        raise DownstreamContinuationError(
            "additional-only continuation ledger identity changed"
        )
    if (
        state.get("first_started_epoch") != payload["first_started_epoch"]
        or state.get("absolute_deadline_epoch") != payload["absolute_deadline_epoch"]
    ):
        raise DownstreamContinuationError("continuation clock changed")
    if time.time() >= _time(
        payload["absolute_deadline_epoch"], "continuation deadline"
    ):
        raise DownstreamContinuationError(
            "current downstream continuation has expired; no automatic reauthorization"
        )
    for name in _COUNTERS:
        _count(state.get(name), name)
    for cap, used, reserved in (
        ("max_provider_calls_total", "calls_used", "calls_reserved"),
        ("max_output_tokens_total", "output_tokens_used", "output_tokens_reserved"),
        ("max_retry_attempts_total", "retry_attempts_used", "retry_attempts_reserved"),
    ):
        if state[used] + _count(state.get(reserved), reserved) > getattr(budget, cap):
            raise DownstreamContinuationError(
                "additional ledger exceeds its finite allowance"
            )
    used_records = [*roots, record, *([head] if head is not None else [])]
    immutable_files = {
        **{r.path: r.content_hash for r in used_records},
        config["config_path"]: config["config_sha256"],
        old_pin["state_path"]: old_pin["state_sha256"],
        old_pin["identity_path"]: old_pin["identity_sha256"],
        str(state_path.with_name(state_path.name + ".identity.json")): payload[
            "state_identity_sha256"
        ],
    }
    if any(file_sha256(path) != expected for path, expected in immutable_files.items()):
        raise DownstreamContinuationError(
            "continuation source bytes changed during load"
        )
    final_revision, fresh_records = registry.capture_record_snapshot()
    fresh = {r.artifact_id: _sha(asdict(r)) for r in fresh_records}
    if final_revision < initial_revision or any(
        fresh.get(r.artifact_id) != _sha(asdict(r)) for r in used_records
    ):
        raise DownstreamContinuationError(
            "continuation scope records changed during load"
        )
    from types import SimpleNamespace

    fields: dict[str, Any] = {
        "job_id": workspace.job_id,
        "budget": budget,
        "state_path": state_path,
        "run_id": payload["run_id"],
        "artifact_record": record,
        "original_spec_budget_hash": payload["original_spec_budget_hash"],
        "original_scope_hashes": payload["original_scope_hashes"],
        "carried_usage": payload["carried_usage"],
        "first_started_epoch": payload["first_started_epoch"],
        "absolute_deadline_epoch": payload["absolute_deadline_epoch"],
        "allow_provider_stages": ALLOW_PROVIDER_STAGES,
    }
    return VerifiedDownstreamContinuation(
        **fields,
        _verification_token=_TOKEN,
        _verified_fields_sha256=_verified_fields(SimpleNamespace(**fields)),
    )


def load_current_continuation(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    spec_payload: Mapping[str, Any] | None = None,
    *,
    expected_scope_hashes: Mapping[str, str] | None = None,
) -> VerifiedDownstreamContinuation | None:
    _workspace_check(workspace, registry)
    registry.reload()
    pointer = registry.get(CURRENT_ID)
    if pointer is None:
        return None
    pointer = _ready(registry, CURRENT_ID)
    validate_continuation_artifact(pointer, Path(pointer.path))
    payload = _object(Path(pointer.path))
    record = _ready(registry, str(payload.get("grant_artifact_id") or ""))
    if record.content_hash != payload.get("grant_file_sha256") or tuple(
        pointer.depends_on
    ) != (ArtifactDependencyRefV2.from_record(record),):
        raise DownstreamContinuationError(
            "continuation current head is stale or foreign"
        )
    return _load_grant(
        workspace, registry, record, spec_payload, expected_scope_hashes, head=pointer
    )


def prepare_downstream_continuation(
    workspace: JobWorkspace,
    registry: ArtifactRegistry,
    *,
    budget: ProviderAggregateBudgetV2 | Mapping[str, Any],
    actor: str,
    reason: str,
    authorization_reference: str,
    spec_payload: Mapping[str, Any] | None = None,
) -> VerifiedDownstreamContinuation:
    _workspace_check(workspace, registry)
    approved = (
        budget
        if isinstance(budget, ProviderAggregateBudgetV2)
        else ProviderAggregateBudgetV2.from_mapping(budget)
    )
    if (
        approved.max_wall_seconds <= 0
        or not actor.strip()
        or not reason.strip()
        or not authorization_reference.strip()
    ):
        raise DownstreamContinuationError(
            "finite wall and explicit actor/reason/human authorization reference required"
        )
    with AttemptExecutionLease(workspace):
        existing = load_current_continuation(workspace, registry, spec_payload)
        if existing is not None:
            saved = _object(Path(existing.artifact_record.path))
            if existing.budget != approved or any(
                saved.get(name) != value
                for name, value in (
                    ("actor", actor),
                    ("reason", reason),
                    ("authorization_reference", authorization_reference),
                )
            ):
                raise DownstreamContinuationError(
                    "current live grant already owns this parent; reuse exact authority only"
                )
            return existing
        old, _identity, old_pin = _parent(workspace)
        parent_budget = ProviderAggregateBudgetV2.from_mapping(old["budget"])
        for cap, usage in (
            ("max_provider_calls_total", "calls_used"),
            ("max_output_tokens_total", "output_tokens_used"),
            ("max_retry_attempts_total", "retry_attempts_used"),
        ):
            if getattr(approved, cap) > getattr(parent_budget, cap) - old[usage]:
                raise DownstreamContinuationError(
                    "continuation budget exceeds original unspent aggregate allowance"
                )
        roots, spec, envelope, config = _scope(registry, spec_payload)
        if (
            ProviderAggregateBudgetV2.from_mapping(spec["metadata"]["provider_budget"])
            != parent_budget
            or envelope.provider_budget != parent_budget
        ):
            raise DownstreamContinuationError(
                "parent/spec/envelope budget limits differ"
            )
        checked = tuple(registry.verify_ready_artifact_closures(roots))
        if [_sha(asdict(r)) for r in checked] != [_sha(asdict(r)) for r in roots]:
            raise DownstreamContinuationError(
                "scope records changed during preparation"
            )
        grant_id = _sha({"job_id": workspace.job_id, "parent_ledger": old_pin})
        artifact_id = f"downstream-continuation:{grant_id}"
        orphan = registry.get(artifact_id)
        if orphan is None:
            state_path = Path(
                workspace.log_path(f"downstream_continuations/{grant_id}.json")
            )
            if (
                state_path.exists()
                or state_path.with_name(state_path.name + ".identity.json").exists()
            ):
                raise DownstreamContinuationError(
                    "continuation ledger exists without its immutable grant"
                )
            run_id = f"continuation:{workspace.job_id}:{grant_id}"
            controller = ProviderBudgetController(approved)
            controller.bind_state_path(
                state_path, acceptance_run_id=run_id, state_started=False
            )
            state = _object(state_path)
            original = ProviderAggregateBudgetV2.from_mapping(
                spec["metadata"]["provider_budget"]
            )
            pins = {
                r.artifact_id: {"ref": _ref(r), "record_sha256": _sha(asdict(r))}
                for r in roots
            }
            payload = {
                "schema_version": f"{GRANT_TYPE}/v1",
                "job_id": workspace.job_id,
                "grant_id": grant_id,
                "run_id": run_id,
                "budget": approved.to_dict(),
                "state_path": str(state_path),
                "state_identity_sha256": file_sha256(
                    state_path.with_name(state_path.name + ".identity.json")
                ),
                "first_started_epoch": state["first_started_epoch"],
                "absolute_deadline_epoch": state["absolute_deadline_epoch"],
                "actor": actor,
                "reason": reason,
                "authorization_reference": authorization_reference,
                "allow_provider_stages": list(ALLOW_PROVIDER_STAGES),
                "accounting_semantics": "additional_only_v5",
                "carried_usage": {name: old[name] for name in _COUNTERS},
                "parent_ledger": old_pin,
                "original_spec_budget_hash": hash_json(original.to_dict()),
                "original_scope_hashes": dict(envelope.scope_hashes),
                "scope_records": pins,
                "config": config,
            }
            payload["grant_sha256"] = _sha(payload)
            registry.reload()
            if registry.get(CURRENT_ID) is not None:
                raise DownstreamContinuationError(
                    "continuation head appeared during preparation"
                )
            orphan = publish_json_artifact(
                LocalPublicationContext(),
                registry,
                workspace.artifact_path(f"downstream_continuations/{grant_id}.json"),
                payload,
                artifact_id=artifact_id,
                artifact_role=GRANT_TYPE,
                artifact_type=GRANT_TYPE,
                artifact_version="v1",
                producer="runtime.downstream_continuation",
                depends_on=[ArtifactDependencyRefV2.from_record(r) for r in roots],
                expected_revision=registry.revision,
            )
        verified = _load_grant(workspace, registry, orphan, spec_payload, None)
        payload = _object(Path(orphan.path))
        if verified.budget != approved or any(
            payload.get(name) != value
            for name, value in (
                ("actor", actor),
                ("reason", reason),
                ("authorization_reference", authorization_reference),
            )
        ):
            raise DownstreamContinuationError(
                "existing parent grant cannot be replaced or forked"
            )
        registry.reload()
        if registry.get(CURRENT_ID) is not None:
            raise DownstreamContinuationError(
                "continuation current head changed before CAS"
            )
        publish_json_artifact(
            LocalPublicationContext(),
            registry,
            workspace.artifact_path("downstream_continuations/current.json"),
            {
                "schema_version": f"{POINTER_TYPE}/v1",
                "job_id": workspace.job_id,
                "grant_artifact_id": orphan.artifact_id,
                "grant_file_sha256": orphan.content_hash,
            },
            artifact_id=CURRENT_ID,
            artifact_role=POINTER_TYPE,
            artifact_type=POINTER_TYPE,
            artifact_version="v1",
            producer="runtime.downstream_continuation",
            depends_on=[ArtifactDependencyRefV2.from_record(orphan)],
            expected_revision=registry.revision,
        )
        loaded = load_current_continuation(workspace, registry, spec_payload)
        if loaded is None:
            raise DownstreamContinuationError("continuation head publication failed")
        return loaded


def current_continuation() -> VerifiedDownstreamContinuation | None:
    return _CURRENT.get()


@contextmanager
def bind_current_continuation(
    verified: VerifiedDownstreamContinuation,
) -> Iterator[VerifiedDownstreamContinuation]:
    if (
        not isinstance(verified, VerifiedDownstreamContinuation)
        or verified._verification_token is not _TOKEN
    ):
        raise DownstreamContinuationError("only a verified continuation may be bound")
    if verified._verified_fields_sha256 != _verified_fields(verified):
        raise DownstreamContinuationError("bound continuation fields changed")
    token = _CURRENT.set(verified)
    try:
        yield verified
    finally:
        _CURRENT.reset(token)


def validate_continuation_controller(
    verified: VerifiedDownstreamContinuation,
    controller: ProviderBudgetController,
    *,
    runtime_spec_budget: ProviderAggregateBudgetV2 | Mapping[str, Any] | None = None,
) -> bool:
    if (
        not isinstance(verified, VerifiedDownstreamContinuation)
        or verified._verification_token is not _TOKEN
    ):
        raise DownstreamContinuationError("continuation proof is not verified")
    if verified._verified_fields_sha256 != _verified_fields(verified):
        raise DownstreamContinuationError("continuation proof fields changed")
    if (
        controller.budget != verified.budget
        or controller.state_path != verified.state_path.resolve()
        or controller.acceptance_run_id != verified.run_id
        or not controller.state_started
        or controller.durable_deadline_epoch != verified.absolute_deadline_epoch
    ):
        raise DownstreamContinuationError(
            "controller does not match the additional-only continuation"
        )
    if time.time() >= verified.absolute_deadline_epoch:
        raise DownstreamContinuationError("downstream continuation has expired")
    if runtime_spec_budget is not None:
        original = (
            runtime_spec_budget
            if isinstance(runtime_spec_budget, ProviderAggregateBudgetV2)
            else ProviderAggregateBudgetV2.from_mapping(runtime_spec_budget)
        )
        if hash_json(original.to_dict()) != verified.original_spec_budget_hash:
            raise DownstreamContinuationError(
                "continuation belongs to another original spec budget"
            )
    return True


def validate_current_continuation_controller(
    controller: ProviderBudgetController,
    *,
    runtime_spec_budget: ProviderAggregateBudgetV2 | Mapping[str, Any] | None = None,
) -> bool:
    verified = current_continuation()
    return (
        False
        if verified is None
        else validate_continuation_controller(
            verified, controller, runtime_spec_budget=runtime_spec_budget
        )
    )


def controller_for_continuation(
    verified: VerifiedDownstreamContinuation,
) -> ProviderBudgetController:
    if (
        not isinstance(verified, VerifiedDownstreamContinuation)
        or verified._verification_token is not _TOKEN
    ):
        raise DownstreamContinuationError("continuation proof is not verified")
    if verified._verified_fields_sha256 != _verified_fields(verified):
        raise DownstreamContinuationError("continuation proof fields changed")
    controller = ProviderBudgetController(verified.budget)
    controller.bind_state_path(
        verified.state_path, acceptance_run_id=verified.run_id, state_started=True
    )
    validate_continuation_controller(verified, controller)
    controller._verified_downstream_continuation = verified
    return controller

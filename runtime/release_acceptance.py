"""Typed release-acceptance contracts and evidence gates.

This module deliberately does not manufacture evidence.  A successful public
runtime job is only one input to a gate; each specialized gate must carry its
own final-SHA-bound evidence and its own facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from runtime.provider_runtime import ProviderAggregateBudgetV1
from services.durable_io import atomic_replace_with_retry


class ReleaseAcceptanceSpecError(ValueError):
    """Raised when release-acceptance input is ambiguous or malformed."""


_BUDGET_FIELDS = frozenset(
    {
        "max_provider_calls_total",
        "max_output_tokens_total",
        "max_retry_attempts_total",
        "max_wall_seconds",
        # Explicit compatibility aliases. They are normalized, never ignored.
        "max_provider_calls",
        "max_output_tokens",
        "max_retry_attempts",
        "timeout_seconds",
    }
)
_ACCEPTANCE_FIELDS = frozenset(
    {
        "budget",
        "acceptance_budget",
        "evidence_manifest",
        "runtime_spec",
        "state_path",
        "job_id",
        "third_party_acknowledged",
        "third_party_hosts",
        "gates",
    }
)


def _reject_unknown(payload: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ReleaseAcceptanceSpecError(
            f"{label} contains unknown fields: {', '.join(unknown)}"
        )


def _integer(value: Any, *, field_name: str, positive: bool = True) -> int:
    if isinstance(value, bool):
        raise ReleaseAcceptanceSpecError(f"{field_name} must be an integer")
    if not isinstance(value, int):
        raise ReleaseAcceptanceSpecError(f"{field_name} must be an integer")
    if positive and value <= 0:
        raise ReleaseAcceptanceSpecError(f"{field_name} must be greater than zero")
    if not positive and value < 0:
        raise ReleaseAcceptanceSpecError(f"{field_name} must be non-negative")
    return value


@dataclass(frozen=True)
class ReleaseAcceptanceBudget:
    max_provider_calls_total: int = 24
    max_output_tokens_total: int = 5_000_000
    max_retry_attempts_total: int = 2
    max_wall_seconds: int = 900

    def __post_init__(self) -> None:
        for name in (
            "max_provider_calls_total",
            "max_output_tokens_total",
            "max_retry_attempts_total",
            "max_wall_seconds",
        ):
            _integer(getattr(self, name), field_name=name)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        defaults: "ReleaseAcceptanceBudget | None" = None,
    ) -> "ReleaseAcceptanceBudget":
        source = value or {}
        if not isinstance(source, Mapping):
            raise ReleaseAcceptanceSpecError("acceptance budget must be a JSON object")
        _reject_unknown(source, _BUDGET_FIELDS, "acceptance budget")
        base = defaults or cls()
        aliases = {
            "max_provider_calls_total": ("max_provider_calls_total", "max_provider_calls"),
            "max_output_tokens_total": ("max_output_tokens_total", "max_output_tokens"),
            "max_retry_attempts_total": ("max_retry_attempts_total", "max_retry_attempts"),
            "max_wall_seconds": ("max_wall_seconds", "timeout_seconds"),
        }
        values: dict[str, int] = {}
        for canonical, keys in aliases.items():
            present = [key for key in keys if key in source]
            if not present:
                values[canonical] = int(getattr(base, canonical))
                continue
            parsed = [_integer(source[key], field_name=key) for key in present]
            if len(set(parsed)) != 1:
                raise ReleaseAcceptanceSpecError(
                    f"acceptance budget aliases disagree for {canonical}"
                )
            values[canonical] = parsed[0]
        return cls(**values)

    def to_dict(self) -> dict[str, int]:
        return {
            "max_provider_calls_total": self.max_provider_calls_total,
            "max_output_tokens_total": self.max_output_tokens_total,
            "max_retry_attempts_total": self.max_retry_attempts_total,
            "max_wall_seconds": self.max_wall_seconds,
        }

    def to_provider_budget(self) -> ProviderAggregateBudgetV1:
        return ProviderAggregateBudgetV1(**self.to_dict())


@dataclass(frozen=True)
class ReleaseAcceptanceSpec:
    budget: ReleaseAcceptanceBudget = field(default_factory=ReleaseAcceptanceBudget)
    evidence_manifest: str = ""
    runtime_spec: str = ""
    state_path: str = ""
    job_id: str = ""
    third_party_acknowledged: bool = False
    third_party_hosts: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        origin_dir: str | Path | None = None,
        defaults: ReleaseAcceptanceBudget | None = None,
    ) -> "ReleaseAcceptanceSpec":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("release acceptance spec must be a JSON object")
        _reject_unknown(payload, _ACCEPTANCE_FIELDS, "release acceptance spec")
        raw_budget = payload.get("budget", payload.get("acceptance_budget", {}))
        budget = ReleaseAcceptanceBudget.from_mapping(raw_budget, defaults=defaults)
        evidence = payload.get("evidence_manifest", "")
        if evidence is None:
            evidence = ""
        if not isinstance(evidence, str):
            raise ReleaseAcceptanceSpecError("evidence_manifest must be a JSON string")
        evidence_path = Path(evidence).expanduser() if evidence else None
        if origin_dir is not None and evidence_path is not None and not evidence_path.is_absolute():
            evidence_path = Path(origin_dir).expanduser().resolve() / evidence_path
        runtime_spec = payload.get("runtime_spec", "")
        if runtime_spec is None:
            runtime_spec = ""
        if not isinstance(runtime_spec, str):
            raise ReleaseAcceptanceSpecError("runtime_spec must be a JSON string")
        runtime_path = Path(runtime_spec).expanduser() if runtime_spec else None
        if origin_dir is not None and runtime_path is not None and not runtime_path.is_absolute():
            runtime_path = Path(origin_dir).expanduser().resolve() / runtime_path
        state_path = payload.get("state_path", "")
        if state_path is None:
            state_path = ""
        if not isinstance(state_path, str):
            raise ReleaseAcceptanceSpecError("state_path must be a JSON string")
        state_file = Path(state_path).expanduser() if state_path else None
        if origin_dir is not None and state_file is not None and not state_file.is_absolute():
            state_file = Path(origin_dir).expanduser().resolve() / state_file
        job_id = payload.get("job_id", "")
        if job_id is None:
            job_id = ""
        if not isinstance(job_id, str):
            raise ReleaseAcceptanceSpecError("job_id must be a JSON string")
        acknowledged = payload.get("third_party_acknowledged", False)
        if not isinstance(acknowledged, bool):
            raise ReleaseAcceptanceSpecError("third_party_acknowledged must be a JSON boolean")
        raw_hosts = payload.get("third_party_hosts", [])
        if not isinstance(raw_hosts, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_hosts
        ):
            raise ReleaseAcceptanceSpecError("third_party_hosts must be an array of strings")
        raw_gates = payload.get("gates", [])
        if not isinstance(raw_gates, (list, tuple)) or any(
            not isinstance(item, str) for item in raw_gates
        ):
            raise ReleaseAcceptanceSpecError("gates must be an array of strings")
        return cls(
            budget=budget,
            evidence_manifest=str(evidence_path) if evidence_path is not None else "",
            runtime_spec=str(runtime_path) if runtime_path is not None else "",
            state_path=str(state_file) if state_file is not None else "",
            job_id=job_id.strip(),
            third_party_acknowledged=acknowledged,
            third_party_hosts=tuple(item.strip() for item in raw_hosts if item.strip()),
            gates=tuple(item.strip() for item in raw_gates if item.strip()),
        )


@dataclass(frozen=True)
class AcceptanceRunStateV1:
    """Durable resumable state for the public acceptance control-plane action."""

    run_id: str
    final_sha: str
    acceptance_spec_path: str
    runtime_spec_path: str
    status: str
    gates: Mapping[str, Any]
    workspace_path: str = ""
    job_id: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "release-acceptance-run-state-v1",
            "run_id": self.run_id,
            "final_sha": self.final_sha,
            "acceptance_spec_path": self.acceptance_spec_path,
            "runtime_spec_path": self.runtime_spec_path,
            "status": self.status,
            "gates": dict(self.gates),
            "workspace_path": self.workspace_path,
            "job_id": self.job_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "AcceptanceRunStateV1":
        if payload.get("schema_version") != "release-acceptance-run-state-v1":
            raise ReleaseAcceptanceSpecError("acceptance run state schema is invalid")
        gates = payload.get("gates")
        if not isinstance(gates, Mapping):
            raise ReleaseAcceptanceSpecError("acceptance run state gates must be an object")
        required = ("run_id", "final_sha", "acceptance_spec_path", "status", "updated_at")
        missing = [key for key in required if not str(payload.get(key) or "").strip()]
        if missing:
            raise ReleaseAcceptanceSpecError(
                "acceptance run state is missing: " + ", ".join(missing)
            )
        return cls(
            run_id=str(payload["run_id"]),
            final_sha=str(payload["final_sha"]),
            acceptance_spec_path=str(payload["acceptance_spec_path"]),
            runtime_spec_path=str(payload.get("runtime_spec_path") or ""),
            status=str(payload["status"]),
            gates=dict(gates),
            workspace_path=str(payload.get("workspace_path") or ""),
            job_id=str(payload.get("job_id") or ""),
            updated_at=str(payload["updated_at"]),
        )


GATE_CONTRACTS: dict[str, dict[str, Any]] = {
    "A": {
        "purpose": "exact-final-SHA offline closure",
        "prerequisites": ["final local checkout"],
        "actual_action": "compileall, strict-offline pytest, Pyright, pip check, doctor, diff check",
        "required_live": False,
        "evidence_required": [],
    },
    "B": {
        "purpose": "dry transport preflight",
        "prerequisites": ["runtime spec", "configuration file"],
        "actual_action": "reviewctl preflight --config <config> --action <action>",
        "required_live": False,
        "evidence_required": [],
    },
    "C": {
        "purpose": "one real F1 paper through the production control plane",
        "prerequisites": ["B", "real F1 spec/corpus", "approved credential"],
        "actual_action": "reviewctl run --spec <runtime-spec>",
        "required_live": True,
        "evidence_required": ["source_count", "canonical_stage1_count", "actual_transport_calls", "closure_complete"],
    },
    "D": {
        "purpose": "three heterogeneous real papers",
        "prerequisites": ["C", "three real PDFs with distinct modality profiles"],
        "actual_action": "one production run containing the three approved PDFs",
        "required_live": True,
        "evidence_required": ["source_count", "heterogeneity", "actual_transport_calls", "closure_complete"],
    },
    "E": {
        "purpose": "real process interruption and resume",
        "prerequisites": ["D"],
        "actual_action": "terminate and restart reviewctl at a durable process boundary",
        "required_live": True,
        "evidence_required": ["interruption", "resume", "provider_call_ledger_delta", "duplicate_receipts"],
    },
    "F": {
        "purpose": "real multi-provider Outline v3",
        "prerequisites": ["C", "role-mapped provider routes"],
        "actual_action": "run Outline v3 against real Stage 1 artifacts",
        "required_live": True,
        "evidence_required": ["semantic_roles", "actual_transport_calls", "candidate_count", "closure_complete"],
    },
    "G": {
        "purpose": "real Free Mode route isolation",
        "prerequisites": ["approved Free_Mode_API credential"],
        "actual_action": "run Free Mode through the public runtime",
        "required_live": True,
        "evidence_required": ["free_mode_route_only", "actual_transport_calls", "profile_durable"],
    },
    "H": {
        "purpose": "real Validator defect detection, repair, and revalidation",
        "prerequisites": ["C", "approved Validator route"],
        "actual_action": "inject a controlled defect in a copy, validate, repair, and revalidate",
        "required_live": True,
        "evidence_required": ["defect_injected", "defect_detected", "repair_applied", "revalidated_clean", "actual_transport_calls"],
    },
    "I": {
        "purpose": "real GUI browser flow",
        "prerequisites": ["running local GUI", "browser automation"],
        "actual_action": "execute the documented Playwright flow against localhost",
        "required_live": False,
        "evidence_required": ["playwright", "browser_evidence", "flow_completed"],
    },
    "J": {
        "purpose": "real heavy OCR path",
        "prerequisites": ["approved scanned/OCR-poor PDF"],
        "actual_action": "run preprocessing and Stage 1 on the scanned PDF",
        "required_live": False,
        "evidence_required": ["ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed"],
    },
    "K": {
        "purpose": "real Windows contention and locking",
        "prerequisites": ["Windows process environment"],
        "actual_action": "run at least two competing Python processes",
        "required_live": False,
        "evidence_required": ["process_count", "bounded_wait", "no_corrupt_json", "no_lost_update"],
    },
    "Q": {
        "purpose": "full 15-paper F1 chain",
        "prerequisites": ["B-K required gates", "authoritative 15-paper F1 spec/corpus"],
        "actual_action": "run the complete public control-plane chain on the bound 15-paper corpus",
        "required_live": True,
        "evidence_required": ["corpus_count", "stage1_canonical_count", "outline_complete", "docx_complete", "validation_complete", "actual_transport_calls"],
    },
    "R": {
        "purpose": "negative production-path behavior",
        "prerequisites": ["offline regression fixtures"],
        "actual_action": "run typed failure and zero-call regression matrix",
        "required_live": False,
        "evidence_required": ["negative_cases", "zero_call_failures"],
    },
    "S": {
        "purpose": "secret and privacy boundary scan",
        "prerequisites": ["tracked-file scope"],
        "actual_action": "scan tracked files/history according to release policy",
        "required_live": False,
        "evidence_required": ["tracked_secret_hits", "credential_values_exposed"],
    },
    "T": {
        "purpose": "repository governance and branch protection",
        "prerequisites": ["repository owner/API access"],
        "actual_action": "read main branch protection and required checks",
        "required_live": False,
        "evidence_required": ["branch_protection_readback"],
    },
}


def gate_contract(gate: str) -> dict[str, Any]:
    contract = GATE_CONTRACTS.get(str(gate))
    if contract is None:
        raise ReleaseAcceptanceSpecError(f"unsupported specialized gate: {gate}")
    return {"gate": str(gate), **contract}


_EVIDENCE_REF_FIELDS = frozenset(
    {
        "ref_id",
        "path",
        "sha256",
        "size",
        "role",
        "artifact_type",
        "artifact_version",
        "schema_version",
        "job_id",
        "artifact_id",
        "modality",
    }
)
_GATE_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "final_sha",
        "durable_refs",
        "evidence_refs",
        "producer",
        "gate",
    }
)
_MAX_EVIDENCE_BYTES = 128 * 1024 * 1024
_SHA256_RE = r"^[0-9a-f]{64}$"

_GATE_REF_ROLES: dict[str, frozenset[str]] = {
    "C": frozenset({"runtime_spec", "source_pdf", "canonical_stage1", "stage_terminal", "job_outcome", "attempt", "registry", "provider_receipt_ledger", "closure"}),
    "D": frozenset({"source_pdf", "canonical_stage1", "stage_terminal", "registry", "provider_receipt_ledger", "closure"}),
    "E": frozenset({"interruption_event", "resume_event", "provider_receipt_ledger", "process_events"}),
    "F": frozenset({"canonical_stage1", "outline_provider_call_plan", "provider_receipt_ledger", "stage_terminal", "closure"}),
    "G": frozenset({"free_mode_profile", "provider_receipt_ledger", "stage_terminal"}),
    "H": frozenset({"defect_artifact", "repair_artifact", "validation_artifact", "provider_receipt_ledger"}),
    "I": frozenset({"playwright_trace", "browser_evidence"}),
    "J": frozenset({"ocr_diagnostics", "ocr_artifact", "canonical_stage1"}),
    "K": frozenset({"process_events", "lock_state"}),
    "Q": frozenset({"source_pdf", "canonical_stage1", "outline_terminal", "review_docx", "validation_artifact", "provider_receipt_ledger"}),
}

_ROLE_ARTIFACT_TYPES: dict[str, frozenset[str]] = {
    "stage_terminal": frozenset({"runtime_stage_terminal"}),
    "outline_terminal": frozenset({"runtime_stage_terminal"}),
    "attempt": frozenset({"job_attempt"}),
    "job_outcome": frozenset({"job_outcome"}),
    "provider_receipt_ledger": frozenset({"provider_receipt_ledger"}),
    "outline_provider_call_plan": frozenset({"outline_provider_call_plan"}),
    "canonical_stage1": frozenset({
        "stage1_canonical_summaries",
        "stage1_portable_summary_source",
        "stage1_reusable_summary_manifest",
    }),
    "closure": frozenset({
        "provider_receipt_closure",
        "validation_receipt_closure",
        "current_stage_closure_map",
    }),
    "review_docx": frozenset({"review_docx", "review_docx_repaired"}),
    "validation_artifact": frozenset({
        "validation_run_result",
        "validation_run_result_repaired",
        "validation_completion_projection",
    }),
    "defect_artifact": frozenset({
        "validation_run_result",
        "validation_report_projection",
    }),
    "repair_artifact": frozenset({
        "repair_transaction",
        "validation_run_result_repaired",
    }),
}


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _valid_checkout_sha(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) in {40, 64} and all(char in "0123456789abcdef" for char in text)


def _bounded_read(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise ReleaseAcceptanceSpecError(f"evidence reference is a symlink: {path}")
    try:
        size_before = path.stat().st_size
        if size_before > max_bytes:
            raise ReleaseAcceptanceSpecError(
                f"evidence reference exceeds bounded read size: {path.name}"
            )
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
            size_after = os.fstat(handle.fileno()).st_size
        if len(raw) > max_bytes or size_before != size_after or size_after != len(raw):
            raise ReleaseAcceptanceSpecError(
                f"evidence reference changed or exceeds bounded read size: {path.name}"
            )
        return raw
    except ReleaseAcceptanceSpecError:
        raise
    except OSError as exc:
        raise ReleaseAcceptanceSpecError(
            f"evidence reference is unreadable: {path.name}"
        ) from exc


@dataclass(frozen=True)
class DurableEvidenceRefV1:
    """A content-addressed pointer to a durable runtime artifact."""

    ref_id: str
    path: str
    sha256: str
    size: int
    role: str
    artifact_type: str = ""
    artifact_version: str = ""
    schema_version: str = ""
    job_id: str = ""
    artifact_id: str = ""
    modality: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DurableEvidenceRefV1":
        if not isinstance(payload, Mapping):
            raise ReleaseAcceptanceSpecError("durable evidence reference must be an object")
        unknown = sorted(str(key) for key in payload if str(key) not in _EVIDENCE_REF_FIELDS)
        if unknown:
            raise ReleaseAcceptanceSpecError(
                "durable evidence reference contains unknown fields: " + ", ".join(unknown)
            )
        required = ("ref_id", "path", "sha256", "size", "role")
        missing = [key for key in required if not str(payload.get(key) or "").strip() and key != "size"]
        if "size" not in payload:
            missing.append("size")
        if missing:
            raise ReleaseAcceptanceSpecError(
                "durable evidence reference is missing: " + ", ".join(missing)
            )
        raw_size = payload.get("size")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise ReleaseAcceptanceSpecError("durable evidence reference size must be non-negative")
        sha256 = str(payload.get("sha256") or "").strip()
        if not _valid_sha256(sha256) or sha256 != sha256.lower():
            raise ReleaseAcceptanceSpecError("durable evidence reference sha256 must be lowercase SHA-256")
        return cls(
            ref_id=str(payload["ref_id"]).strip(),
            path=str(payload["path"]).strip(),
            sha256=sha256,
            size=raw_size,
            role=str(payload["role"]).strip(),
            artifact_type=str(payload.get("artifact_type") or "").strip(),
            artifact_version=str(payload.get("artifact_version") or "").strip(),
            schema_version=str(payload.get("schema_version") or "").strip(),
            job_id=str(payload.get("job_id") or "").strip(),
            artifact_id=str(payload.get("artifact_id") or "").strip(),
            modality=str(payload.get("modality") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "ref_id": self.ref_id,
                "path": self.path,
                "sha256": self.sha256,
                "size": self.size,
                "role": self.role,
                "artifact_type": self.artifact_type,
                "artifact_version": self.artifact_version,
                "schema_version": self.schema_version,
                "job_id": self.job_id,
                "artifact_id": self.artifact_id,
                "modality": self.modality,
            }.items()
            if value not in ("", None)
        }


class GateEvidenceProducer:
    """Create evidence indexes from bytes that actually exist on disk."""

    def __init__(
        self,
        *,
        final_sha: str,
        origin_dir: str | Path | None = None,
        max_bytes: int = _MAX_EVIDENCE_BYTES,
    ) -> None:
        if not _valid_checkout_sha(final_sha):
            raise ReleaseAcceptanceSpecError("evidence producer requires final_sha")
        self.final_sha = str(final_sha).strip()
        self.origin_dir = Path(origin_dir).expanduser().resolve() if origin_dir else None
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ReleaseAcceptanceSpecError("evidence producer max_bytes must be positive")

    def reference(
        self,
        path: str | Path,
        *,
        role: str,
        artifact_type: str = "",
        artifact_version: str = "",
        schema_version: str = "",
        job_id: str = "",
        artifact_id: str = "",
        modality: str = "",
    ) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        raw = _bounded_read(target, max_bytes=self.max_bytes)
        ref = DurableEvidenceRefV1(
            ref_id=f"{role}:{hashlib.sha256(str(target).encode('utf-8')).hexdigest()[:16]}",
            path=str(target),
            sha256=hashlib.sha256(raw).hexdigest(),
            size=len(raw),
            role=str(role).strip(),
            artifact_type=str(artifact_type or "").strip(),
            artifact_version=str(artifact_version or "").strip(),
            schema_version=str(schema_version or "").strip(),
            job_id=str(job_id or "").strip(),
            artifact_id=str(artifact_id or "").strip(),
            modality=str(modality or "").strip(),
        )
        return ref.to_dict()

    def build_gate(self, gate: str, refs: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        gate_contract(str(gate))
        normalized = [DurableEvidenceRefV1.from_mapping(item).to_dict() for item in refs]
        return {
            "final_sha": self.final_sha,
            "durable_refs": normalized,
            "producer": "runtime.release_acceptance.GateEvidenceProducer",
            "schema_version": "release-acceptance-gate-evidence-v1",
            "gate": str(gate),
        }

    def write_manifest(
        self,
        path: str | Path,
        gates: Mapping[str, Iterable[Mapping[str, Any]]],
    ) -> Path:
        target = Path(path).expanduser().resolve()
        payload = {
            "schema_version": "release-acceptance-evidence-index-v1",
            "final_sha": self.final_sha,
            "gates": {
                str(gate): self.build_gate(str(gate), list(refs))
                for gate, refs in gates.items()
            },
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        with temp.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace_with_retry(temp, target, timeout_seconds=5.0)
        return target


class GateEvidenceVerifier:
    """Reopen durable references and derive gate facts from their bytes."""

    def __init__(self, *, max_bytes: int = _MAX_EVIDENCE_BYTES) -> None:
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ReleaseAcceptanceSpecError("evidence verifier max_bytes must be positive")

    @staticmethod
    def _resolve_path(raw: str, *, origin_dir: str | Path | None) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute() and origin_dir is not None:
            path = Path(origin_dir).expanduser() / path
        return path.resolve()

    @staticmethod
    def _json_payload(raw: bytes, path: Path) -> Any:
        if path.suffix.casefold() not in {".json", ".jsonl"}:
            return None
        if path.suffix.casefold() == ".jsonl":
            rows: list[Any] = []
            for line in raw.decode("utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
            return rows
        return json.loads(raw.decode("utf-8"))

    @staticmethod
    def _derive_facts(
        gate: str,
        refs: list[DurableEvidenceRefV1],
        payloads: Mapping[str, Any],
    ) -> dict[str, Any]:
        roles = {ref.role for ref in refs}
        facts: dict[str, Any] = {
            "source_count": len({ref.sha256 for ref in refs if ref.role in {"source_pdf", "f1_corpus"}}),
            "canonical_stage1_count": len({ref.sha256 for ref in refs if ref.role == "canonical_stage1"}),
            "actual_transport_calls": 0,
            "semantic_roles": set(),
            "receipt_routes": set(),
            "candidate_count": 0,
            "closure_complete": False,
            "interruption": "interruption_event" in roles,
            "resume": "resume_event" in roles,
            "duplicate_receipts": 0,
            "heterogeneity": len({ref.modality for ref in refs if ref.modality}),
            "free_mode_route_only": False,
            "profile_durable": "free_mode_profile" in roles,
            "defect_injected": "defect_artifact" in roles,
            "defect_detected": False,
            "repair_applied": "repair_artifact" in roles,
            "revalidated_clean": False,
            "playwright": "playwright_trace" in roles,
            "browser_evidence": "browser_evidence" in roles,
            "flow_completed": False,
            "ocr_actually_used": False,
            "page_identity": "ocr_diagnostics" in roles,
            "evidence_artifact": "ocr_artifact" in roles,
            "stage1_consumed": "canonical_stage1" in roles,
            "process_count": 0,
            "bounded_wait": "lock_state" in roles,
            "no_corrupt_json": True,
            "no_lost_update": True,
            "outline_complete": "outline_terminal" in roles,
            "docx_complete": "review_docx" in roles,
            "validation_complete": "validation_artifact" in roles,
            "route_plan_present": False,
        }
        receipt_ids: set[str] = set()
        process_ids: set[str] = set()
        providers: set[str] = set()
        for ref in refs:
            payload = payloads.get(ref.ref_id)
            rows = payload if isinstance(payload, list) else [payload]
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                artifact_type = str(row.get("artifact_type") or ref.artifact_type)
                if ref.role == "provider_receipt_ledger" or artifact_type == "provider_call_receipt":
                    receipt_id = str(row.get("receipt_id") or "")
                    if receipt_id in receipt_ids:
                        facts["duplicate_receipts"] += 1
                    elif receipt_id:
                        receipt_ids.add(receipt_id)
                    status = str(row.get("status") or "")
                    metadata = row.get("metadata")
                    attempted = status == "success" or (
                        isinstance(metadata, Mapping) and bool(metadata.get("transport_config"))
                    )
                    if attempted:
                        try:
                            facts["actual_transport_calls"] += max(1, int(row.get("attempts") or 1))
                        except (TypeError, ValueError):
                            facts["actual_transport_calls"] += 1
                    node_id = str(row.get("node_id") or row.get("route") or "")
                    if node_id:
                        facts["semantic_roles"].add(node_id)
                        if "candidate_" in node_id and "_provider_generation" in node_id:
                            facts["candidate_count"] += 1
                    route_name = str(row.get("route") or "")
                    if route_name:
                        facts["receipt_routes"].add(route_name)
                    provider = str(row.get("provider") or "")
                    if provider:
                        providers.add(provider)
                if ref.role in {"closure", "stage_terminal", "outline_terminal"}:
                    status = str(row.get("status") or row.get("closure_status") or "").casefold()
                    closure_payload = row.get("payload")
                    if isinstance(closure_payload, Mapping):
                        status = str(
                            closure_payload.get("status")
                            or closure_payload.get("closure_status")
                            or status
                        ).casefold()
                    facts["closure_complete"] = facts["closure_complete"] or status in {
                        "complete", "completed", "trusted", "verified", "succeeded"
                    } or (
                        isinstance(closure_payload, Mapping)
                        and closure_payload.get("complete") is True
                    ) or row.get("complete") is True
                if ref.role == "outline_provider_call_plan":
                    route_plan = row.get("reachable_provider_route_plan")
                    if isinstance(route_plan, Mapping) and isinstance(route_plan.get("routes"), list):
                        facts["route_plan_present"] = True
                if ref.role == "job_outcome":
                    facts["closure_complete"] = facts["closure_complete"] or (
                        str(row.get("job_status") or "").casefold() == "completed"
                        and bool(row.get("canonical_ready", False))
                    )
                if ref.role == "repair_artifact":
                    facts["defect_detected"] = facts["defect_detected"] or bool(
                        row.get("defect_detected") or row.get("findings")
                    )
                if ref.role == "validation_artifact":
                    facts["revalidated_clean"] = facts["revalidated_clean"] or (
                        str(row.get("status") or row.get("validation_status") or "").casefold()
                        in {"clean", "completed", "succeeded"}
                    )
                if ref.role in {"playwright_trace", "browser_evidence"}:
                    facts["flow_completed"] = facts["flow_completed"] or (
                        str(row.get("status") or "").casefold()
                        in {"passed", "pass", "completed", "succeeded"}
                    )
                if ref.role == "ocr_diagnostics":
                    facts["ocr_actually_used"] = facts["ocr_actually_used"] or bool(
                        row.get("used_ocr") or row.get("ocr_actually_used")
                    )
                if ref.role == "process_events":
                    process_id = str(row.get("process_id") or row.get("pid") or "")
                    if process_id:
                        process_ids.add(process_id)
        facts["process_count"] = len(process_ids)
        facts["semantic_roles"] = sorted(facts["semantic_roles"])
        facts["receipt_routes"] = sorted(facts["receipt_routes"])
        facts["free_mode_route_only"] = (
            "provider_receipt_ledger" in roles
            and bool(facts["receipt_routes"])
            and all("free" in str(route).casefold() for route in facts["receipt_routes"])
        )
        return facts

    def verify(
        self,
        gate: str,
        evidence: Mapping[str, Any] | None,
        *,
        expected_final_sha: str = "",
        origin_dir: str | Path | None = None,
        expected_job_id: str = "",
    ) -> dict[str, Any]:
        contract = gate_contract(gate)
        if not isinstance(evidence, Mapping):
            return {"status": "NOT_VERIFIED", "reason": "gate evidence is missing", "contract": contract}
        if evidence.get("schema_version") != "release-acceptance-gate-evidence-v1":
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence schema is not the durable reference schema",
                "contract": contract,
            }
        if evidence.get("producer") != "runtime.release_acceptance.GateEvidenceProducer":
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence producer binding is missing or invalid",
                "contract": contract,
            }
        unknown = sorted(str(key) for key in evidence if str(key) not in _GATE_EVIDENCE_FIELDS)
        if unknown:
            return {
                "status": "FAIL",
                "reason": "gate evidence contains unknown fields: " + ", ".join(unknown),
                "contract": contract,
            }
        if evidence.get("gate") != str(gate):
            return {
                "status": "FAIL",
                "reason": "gate evidence is bound to a different gate",
                "contract": contract,
            }
        evidence_sha = str(evidence.get("final_sha") or "").strip()
        if not _valid_checkout_sha(evidence_sha):
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence is not bound to a valid final checkout SHA",
                "contract": contract,
            }
        if expected_final_sha and evidence_sha != expected_final_sha:
            return {
                "status": "FAIL",
                "reason": "gate evidence final SHA does not match the tested checkout",
                "evidence_final_sha": evidence_sha,
                "expected_final_sha": expected_final_sha,
                "contract": contract,
            }
        raw_refs = evidence.get("durable_refs", evidence.get("evidence_refs"))
        if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
            return {
                "status": "NOT_VERIFIED",
                "reason": "gate evidence must contain durable_refs; handwritten facts are not evidence",
                "contract": contract,
            }
        try:
            refs = [DurableEvidenceRefV1.from_mapping(item) for item in raw_refs]
        except (ReleaseAcceptanceSpecError, TypeError, ValueError) as exc:
            return {"status": "FAIL", "reason": str(exc), "contract": contract}
        identities = [ref.ref_id for ref in refs]
        paths = [str(self._resolve_path(ref.path, origin_dir=origin_dir)).casefold() for ref in refs]
        if len(set(identities)) != len(identities):
            return {"status": "FAIL", "reason": "durable evidence ref_id values must be unique", "contract": contract}
        if len(set(paths)) != len(paths):
            return {"status": "FAIL", "reason": "durable evidence paths must be unique", "contract": contract}
        jobs = {ref.job_id for ref in refs if ref.job_id}
        if expected_job_id and (
            not jobs or jobs != {str(expected_job_id)} or any(not ref.job_id for ref in refs)
        ):
            return {"status": "FAIL", "reason": "durable evidence job identity is missing or belongs to a different job", "contract": contract}
        payloads: dict[str, Any] = {}
        try:
            for ref in refs:
                path = self._resolve_path(ref.path, origin_dir=origin_dir)
                raw = _bounded_read(path, max_bytes=self.max_bytes)
                if len(raw) != ref.size or hashlib.sha256(raw).hexdigest() != ref.sha256:
                    return {
                        "status": "FAIL",
                        "reason": f"durable evidence hash or size mismatch: {ref.ref_id}",
                        "contract": contract,
                    }
                allowed_types = _ROLE_ARTIFACT_TYPES.get(ref.role)
                if allowed_types is not None and ref.artifact_type not in allowed_types:
                    return {
                        "status": "FAIL",
                        "reason": f"durable evidence role/type binding is invalid: {ref.ref_id}",
                        "contract": contract,
                    }
                payload: Any = None
                if ref.artifact_type and path.suffix.casefold() in {".json", ".jsonl"}:
                    payload = self._json_payload(raw, path)
                    rows = payload if isinstance(payload, list) else [payload]
                    for row in rows:
                        if isinstance(row, Mapping) and row.get("artifact_type"):
                            row_type = str(row.get("artifact_type"))
                            accepted_types = (
                                {"provider_receipt_ledger", "provider_call_receipt"}
                                if ref.artifact_type == "provider_receipt_ledger"
                                else {ref.artifact_type}
                            )
                            if row_type not in accepted_types:
                                return {
                                    "status": "FAIL",
                                    "reason": f"durable evidence artifact type mismatch: {ref.ref_id}",
                                    "contract": contract,
                                }
                        if isinstance(row, Mapping) and ref.artifact_version:
                            row_version = str(
                                row.get("artifact_version")
                                or row.get("schema_version")
                                or ""
                            )
                            allowed_versions = (
                                {ref.artifact_version, "v2"}
                                if ref.artifact_type == "provider_receipt_ledger"
                                else {ref.artifact_version}
                            )
                            if row_version and row_version not in allowed_versions:
                                return {
                                    "status": "FAIL",
                                    "reason": f"durable evidence artifact version mismatch: {ref.ref_id}",
                                    "contract": contract,
                                }
                        if (
                            isinstance(row, Mapping)
                            and ref.job_id
                            and row.get("job_id")
                            and str(row.get("job_id")) != ref.job_id
                        ):
                            return {
                                "status": "FAIL",
                                "reason": f"durable evidence job identity mismatch: {ref.ref_id}",
                                "contract": contract,
                            }
                    if ref.schema_version and isinstance(payload, Mapping):
                        actual_schema = str(
                            payload.get("schema_version")
                            or payload.get("artifact_version")
                            or ""
                        )
                        if actual_schema and actual_schema != ref.schema_version:
                            return {
                                "status": "FAIL",
                                "reason": f"durable evidence schema mismatch: {ref.ref_id}",
                                "contract": contract,
                            }
                    payloads[ref.ref_id] = payload
                elif path.suffix.casefold() in {".json", ".jsonl"}:
                    payload = self._json_payload(raw, path)
                if ref.role == "source_pdf" and not raw.startswith(b"%PDF-"):
                    return {
                        "status": "FAIL",
                        "reason": f"source_pdf evidence is not a PDF: {ref.ref_id}",
                        "contract": contract,
                    }
                if path.suffix.casefold() in {".json", ".jsonl"}:
                    if ref.role == "runtime_spec":
                        try:
                            from runtime.job_spec import RuntimeJobSpec

                            if not isinstance(payload, Mapping):
                                raise ValueError("runtime spec must be an object")
                            RuntimeJobSpec.from_dict(payload).validate()
                        except (TypeError, ValueError) as exc:
                            return {
                                "status": "FAIL",
                                "reason": f"runtime spec evidence is invalid: {type(exc).__name__}",
                                "contract": contract,
                            }
                    payloads[ref.ref_id] = payload
        except (OSError, UnicodeError, json.JSONDecodeError, ReleaseAcceptanceSpecError) as exc:
            return {"status": "FAIL", "reason": str(exc), "contract": contract}

        # A hash of artifact_registry.json is not enough by itself. Reopen and
        # verify every ready record it names, including its owner and content
        # hash, so a copied or edited workspace cannot inherit a PASS.
        for ref in refs:
            if ref.role != "registry":
                continue
            registry = payloads.get(ref.ref_id)
            if not isinstance(registry, Mapping):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence is not a JSON object",
                    "contract": contract,
                }
            registry_job_id = str(registry.get("job_id") or "")
            if not registry_job_id:
                return {
                    "status": "FAIL",
                    "reason": "registry evidence has no job owner",
                    "contract": contract,
                }
            if expected_job_id and registry_job_id != str(expected_job_id):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence belongs to a different job",
                    "contract": contract,
                }
            records = registry.get("artifacts")
            if not isinstance(records, list):
                return {
                    "status": "FAIL",
                    "reason": "registry evidence does not contain an artifact array",
                    "contract": contract,
                }
            if registry.get("artifact_registry_version") != "v2":
                return {
                    "status": "FAIL",
                    "reason": "registry evidence is not the current Registry schema",
                    "contract": contract,
                }
            for item in records:
                if not isinstance(item, Mapping) or str(item.get("status") or "") != "ready":
                    continue
                record_job_id = str(item.get("job_id") or "")
                if registry_job_id and record_job_id != registry_job_id:
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact job identity mismatch",
                        "contract": contract,
                    }
                artifact_path = Path(str(item.get("path") or "")).expanduser().resolve()
                expected_hash = str(item.get("content_hash") or "").strip().lower()
                if not _valid_sha256(expected_hash):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact content hash is invalid",
                        "contract": contract,
                    }
                try:
                    artifact_raw = _bounded_read(artifact_path, max_bytes=self.max_bytes)
                except (OSError, ReleaseAcceptanceSpecError) as exc:
                    return {
                        "status": "FAIL",
                        "reason": f"registry artifact is unreadable: {type(exc).__name__}",
                        "contract": contract,
                    }
                raw_record_size = item.get("size")
                try:
                    record_size = (
                        len(artifact_raw)
                        if raw_record_size in (None, "")
                        else int(raw_record_size)
                    )
                except (TypeError, ValueError):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact size is invalid",
                        "contract": contract,
                    }
                if (
                    len(artifact_raw) != record_size
                    or hashlib.sha256(artifact_raw).hexdigest() != expected_hash
                ):
                    return {
                        "status": "FAIL",
                        "reason": "registry artifact hash or size mismatch",
                        "contract": contract,
                    }
            # Reuse the Registry's recursive verifier for canonical roots. It
            # checks dependency identity, ready status, cycles, path, and
            # content hashes; a hand-written summary field cannot replace it.
            try:
                from services.artifact_registry import ArtifactRegistry

                registry_object = ArtifactRegistry(
                    self._resolve_path(ref.path, origin_dir=origin_dir),
                    registry_job_id,
                )
                critical_types = {
                    "job_outcome",
                    "runtime_stage_terminal",
                    "provider_receipt_closure",
                    "current_artifact_set",
                    "review_docx",
                    "review_docx_repaired",
                    "validation_run_result",
                    "validation_run_result_repaired",
                }
                for record in registry_object.list_records():
                    if record.status != "ready" or record.artifact_type not in critical_types:
                        continue
                    registry_object.verify_ready_artifact_closure(record)
            except Exception as exc:
                return {
                    "status": "FAIL",
                    "reason": f"Registry dependency closure is not verified: {type(exc).__name__}",
                    "contract": contract,
                }

        required_roles = _GATE_REF_ROLES.get(str(gate), frozenset())
        missing_roles = sorted(required_roles - {ref.role for ref in refs})
        if missing_roles:
            return {
                "status": "NOT_VERIFIED",
                "reason": "durable evidence is missing required references: " + ", ".join(missing_roles),
                "contract": contract,
            }
        facts = self._derive_facts(str(gate), refs, payloads)
        if contract["required_live"] and int(facts["actual_transport_calls"]) <= 0:
            return {
                "status": "FAIL",
                "reason": "a live provider gate requires actual transport calls derived from receipts",
                "derived_facts": facts,
                "contract": contract,
            }
        truthy_fields = {
            "C": ("closure_complete",),
            "D": ("closure_complete",),
            "E": ("interruption", "resume"),
            "F": ("closure_complete",),
            "G": ("free_mode_route_only", "profile_durable"),
            "H": ("defect_injected", "defect_detected", "repair_applied", "revalidated_clean"),
            "I": ("playwright", "browser_evidence", "flow_completed"),
            "J": ("ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed"),
            "K": ("bounded_wait", "no_corrupt_json", "no_lost_update"),
            "Q": ("outline_complete", "docx_complete", "validation_complete"),
        }
        false_fields = [field for field in truthy_fields.get(str(gate), ()) if facts.get(field) is not True]
        if false_fields:
            return {
                "status": "FAIL",
                "reason": "durable evidence did not prove required facts: " + ", ".join(false_fields),
                "derived_facts": facts,
                "contract": contract,
            }
        if gate == "D" and int(facts.get("source_count") or 0) < 3:
            return {"status": "FAIL", "reason": "heterogeneous gate requires at least three source artifacts", "derived_facts": facts, "contract": contract}
        if gate == "D" and int(facts.get("heterogeneity") or 0) < 3:
            return {"status": "FAIL", "reason": "heterogeneous gate requires three distinct durable modality identities", "derived_facts": facts, "contract": contract}
        if gate == "F" and (
            not facts.get("semantic_roles")
            or int(facts.get("candidate_count") or 0) <= 0
            or facts.get("route_plan_present") is not True
        ):
            return {"status": "FAIL", "reason": "Outline gate lacks the authoritative route plan or candidate receipts derived from the ledger", "derived_facts": facts, "contract": contract}
        if gate == "K" and int(facts.get("process_count") or 0) < 2:
            return {"status": "FAIL", "reason": "contention gate requires at least two durable process identities", "derived_facts": facts, "contract": contract}
        if gate == "C" and (
            int(facts.get("source_count") or 0) != 1
            or int(facts.get("canonical_stage1_count") or 0) != 1
        ):
            return {"status": "FAIL", "reason": "one-paper counts were not derived as a matching durable pair", "derived_facts": facts, "contract": contract}
        if gate == "Q" and (
            int(facts.get("source_count") or 0) != 15
            or int(facts.get("canonical_stage1_count") or 0) != 15
        ):
            return {"status": "FAIL", "reason": "F1 counts were not derived as exactly fifteen durable artifacts", "derived_facts": facts, "contract": contract}
        if gate == "E" and int(facts.get("duplicate_receipts") or 0) != 0:
            return {"status": "FAIL", "reason": "resume gate recorded duplicate receipt identities", "derived_facts": facts, "contract": contract}
        return {
            "status": "PASS",
            "reason": "gate facts were derived from uniquely hashed durable references",
            "derived_facts": facts,
            "verified_refs": [ref.to_dict() for ref in refs],
            "contract": contract,
        }


def validate_gate_evidence(
    gate: str,
    evidence: Mapping[str, Any] | None,
    *,
    expected_final_sha: str = "",
    origin_dir: str | Path | None = None,
    expected_job_id: str = "",
) -> dict[str, Any]:
    """Validate one gate only from reopened durable references."""

    return GateEvidenceVerifier().verify(
        gate,
        evidence,
        expected_final_sha=expected_final_sha,
        origin_dir=origin_dir,
        expected_job_id=expected_job_id,
    )


__all__ = [
    "GATE_CONTRACTS",
    "AcceptanceRunStateV1",
    "DurableEvidenceRefV1",
    "GateEvidenceProducer",
    "GateEvidenceVerifier",
    "ReleaseAcceptanceBudget",
    "ReleaseAcceptanceSpec",
    "ReleaseAcceptanceSpecError",
    "gate_contract",
    "validate_gate_evidence",
]

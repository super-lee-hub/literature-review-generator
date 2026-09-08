"""Typed release-acceptance contracts and evidence gates.

This module deliberately does not manufacture evidence.  A successful public
runtime job is only one input to a gate; each specialized gate must carry its
own final-SHA-bound evidence and its own facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, cast
import zipfile

from runtime.provider_runtime import (
    AcceptanceExecutionContextV1,
    ProviderAggregateBudgetV1,
    ProviderRuntimeContractError,
    ProviderRuntimeLedger,
)
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
        budget_values: list[ReleaseAcceptanceBudget] = []
        for budget_field in ("budget", "acceptance_budget"):
            if budget_field not in payload:
                continue
            raw_budget = payload[budget_field]
            if not isinstance(raw_budget, Mapping):
                raise ReleaseAcceptanceSpecError(
                    f"{budget_field} must be a JSON object"
                )
            budget_values.append(
                ReleaseAcceptanceBudget.from_mapping(raw_budget, defaults=defaults)
            )
        if len(budget_values) == 2 and budget_values[0] != budget_values[1]:
            raise ReleaseAcceptanceSpecError(
                "acceptance budget aliases disagree"
            )
        budget = budget_values[0] if budget_values else ReleaseAcceptanceBudget.from_mapping(
            {}, defaults=defaults
        )
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
    provider_budget_state_path: str = ""
    evidence_root: str = ""
    process_event_log: str = ""
    evidence_revision: int = 0
    evidence_manifest_hash: str = ""
    scenario_id: str = ""

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
            "provider_budget_state_path": self.provider_budget_state_path,
            "evidence_root": self.evidence_root,
            "process_event_log": self.process_event_log,
            "evidence_revision": self.evidence_revision,
            "evidence_manifest_hash": self.evidence_manifest_hash,
            "scenario_id": self.scenario_id,
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
            provider_budget_state_path=str(payload.get("provider_budget_state_path") or ""),
            evidence_root=str(payload.get("evidence_root") or ""),
            process_event_log=str(payload.get("process_event_log") or ""),
            evidence_revision=int(payload.get("evidence_revision") or 0),
            evidence_manifest_hash=str(payload.get("evidence_manifest_hash") or ""),
            scenario_id=str(payload.get("scenario_id") or ""),
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
        "evidence_required": ["source_count", "derived_modalities", "actual_transport_calls", "closure_complete"],
    },
    "E": {
        "purpose": "real process interruption and resume",
        "prerequisites": ["D"],
        "actual_action": "terminate and restart reviewctl at a durable process boundary",
        "required_live": True,
        "evidence_required": ["interruption", "resume", "provider_call_ledger_delta", "duplicate_receipts", "reexecuted_completed_call_ids"],
    },
    "F": {
        "purpose": "real multi-provider Outline v3",
        "prerequisites": ["C", "role-mapped provider routes"],
        "actual_action": "run Outline v3 against real Stage 1 artifacts",
        "required_live": True,
        "evidence_required": ["required_semantic_roles", "executed_semantic_roles", "actual_transport_calls", "candidate_count", "closure_complete"],
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
        "evidence_required": ["challenge_id", "defect_injected", "defect_detected", "repair_applied", "revalidated_clean", "actual_transport_calls"],
    },
    "I": {
        "purpose": "real GUI browser flow",
        "prerequisites": ["running local GUI", "browser automation"],
        "actual_action": "execute the documented Playwright flow against localhost",
        "required_live": False,
        "evidence_required": ["playwright", "browser_evidence", "trace_archive", "flow_completed"],
    },
    "J": {
        "purpose": "real heavy OCR path",
        "prerequisites": ["approved scanned/OCR-poor PDF"],
        "actual_action": "run preprocessing and Stage 1 on the scanned PDF",
        "required_live": False,
        "evidence_required": ["ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed", "lineage"],
    },
    "K": {
        "purpose": "real Windows contention and locking",
        "prerequisites": ["Windows process environment"],
        "actual_action": "run at least two competing Python processes",
        "required_live": False,
        "evidence_required": ["process_count", "bounded_wait", "no_corrupt_json", "no_lost_update", "derived_facts"],
    },
    "Q": {
        "purpose": "full 15-paper F1 chain",
        "prerequisites": ["B-K required gates", "authoritative 15-paper F1 spec/corpus"],
        "actual_action": "run the complete public control-plane chain on the bound 15-paper corpus",
        "required_live": True,
        "evidence_required": ["corpus_count", "paper_identity_count", "stage1_canonical_count", "outline_complete", "docx_complete", "validation_complete", "actual_transport_calls"],
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
    "D": frozenset({"source_pdf", "modality_profile", "canonical_stage1", "stage_terminal", "registry", "provider_receipt_ledger", "closure"}),
    "E": frozenset({"interruption_event", "resume_event", "provider_receipt_ledger", "process_events"}),
    "F": frozenset({"canonical_stage1", "outline_provider_call_plan", "provider_receipt_ledger", "stage_terminal", "closure"}),
    "G": frozenset({"free_mode_profile", "provider_receipt_ledger", "stage_terminal"}),
    "H": frozenset({"defect_artifact", "repair_artifact", "validation_artifact", "provider_receipt_ledger"}),
    "I": frozenset({"playwright_trace", "browser_evidence"}),
    "J": frozenset({"source_pdf", "ocr_diagnostics", "ocr_artifact", "canonical_stage1", "registry"}),
    "K": frozenset({"process_events", "lock_state"}),
    "Q": frozenset({"source_pdf", "canonical_stage1", "outline_terminal", "review_docx", "validation_artifact", "provider_receipt_ledger", "registry", "closure", "job_outcome", "citation_manifest"}),
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
    "citation_manifest": frozenset({"citation_manifest", "citation_manifest_v3"}),
    "ocr_diagnostics": frozenset({"ocr_diagnostics"}),
    "ocr_artifact": frozenset({"ocr_artifact"}),
    "defect_artifact": frozenset({
        "validation_run_result",
        "validation_report_projection",
        "controlled_defect_challenge",
    }),
    "modality_profile": frozenset({"document_modality_profile"}),
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


@dataclass(frozen=True)
class DocumentModalityProfileV1:
    """Deterministic source diagnostics used by the heterogeneous-paper gate."""

    source_pdf_sha256: str
    total_page_count: int
    text_page_ratio: float
    image_page_ratio: float
    table_count: int
    figure_count: int
    scanned_candidate_page_count: int
    ocr_used_page_count: int
    selected_visual_count: int
    extractor_used: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DocumentModalityProfileV1":
        if payload.get("artifact_type") != "document_modality_profile":
            raise ReleaseAcceptanceSpecError("document modality profile artifact type is invalid")
        if payload.get("schema_version") != "document-modality-profile-v1":
            raise ReleaseAcceptanceSpecError("document modality profile schema is invalid")
        source_hash = str(payload.get("source_pdf_sha256") or "").strip()
        if not _valid_sha256(source_hash):
            raise ReleaseAcceptanceSpecError("document modality profile source hash is invalid")
        integer_values: dict[str, int] = {}
        for name in (
            "total_page_count",
            "table_count",
            "figure_count",
            "scanned_candidate_page_count",
            "ocr_used_page_count",
            "selected_visual_count",
        ):
            raw = payload.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} is invalid"
                )
            integer_values[name] = raw
        ratios: dict[str, float] = {}
        for name in ("text_page_ratio", "image_page_ratio"):
            raw = payload.get(name)
            try:
                value = float(str(raw))
            except (TypeError, ValueError):
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} is invalid"
                ) from None
            if value < 0 or value > 1:
                raise ReleaseAcceptanceSpecError(
                    f"document modality profile {name} must be between zero and one"
                )
            ratios[name] = value
        if integer_values["total_page_count"] <= 0:
            raise ReleaseAcceptanceSpecError("document modality profile must have pages")
        if not str(payload.get("extractor_used") or "").strip():
            raise ReleaseAcceptanceSpecError("document modality profile extractor is missing")
        return cls(
            source_pdf_sha256=source_hash,
            total_page_count=integer_values["total_page_count"],
            text_page_ratio=ratios["text_page_ratio"],
            image_page_ratio=ratios["image_page_ratio"],
            table_count=integer_values["table_count"],
            figure_count=integer_values["figure_count"],
            scanned_candidate_page_count=integer_values["scanned_candidate_page_count"],
            ocr_used_page_count=integer_values["ocr_used_page_count"],
            selected_visual_count=integer_values["selected_visual_count"],
            extractor_used=str(payload["extractor_used"]).strip(),
        )

    @property
    def derived_modality(self) -> str:
        scan_ratio = self.scanned_candidate_page_count / self.total_page_count
        ocr_ratio = self.ocr_used_page_count / self.total_page_count
        if ocr_ratio > 0 or scan_ratio >= 0.25:
            return "ocr_scanned"
        visual_signal = self.image_page_ratio >= 0.5 or (
            self.table_count + self.figure_count
            >= max(1, self.total_page_count // 3)
        )
        return "visual_table_heavy" if visual_signal else "text_heavy"


@dataclass(frozen=True)
class ControlledDefectChallengeV1:
    """Typed challenge lineage for Validator defect injection and repair."""

    challenge_id: str
    baseline_review_artifact_id: str
    baseline_review_hash: str
    mutated_review_path: str
    mutated_review_hash: str
    mutation_type: str
    mutation_locator: str
    original_value_hash: str
    mutated_value_hash: str
    expected_detection_class: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "ControlledDefectChallengeV1":
        if payload.get("artifact_type") != "controlled_defect_challenge":
            raise ReleaseAcceptanceSpecError("controlled defect challenge artifact type is invalid")
        if payload.get("schema_version") != "controlled-defect-challenge-v1":
            raise ReleaseAcceptanceSpecError("controlled defect challenge schema is invalid")
        text_fields = (
            "challenge_id",
            "baseline_review_artifact_id",
            "baseline_review_hash",
            "mutated_review_path",
            "mutated_review_hash",
            "mutation_type",
            "mutation_locator",
            "original_value_hash",
            "mutated_value_hash",
            "expected_detection_class",
        )
        values = {name: str(payload.get(name) or "").strip() for name in text_fields}
        if any(not value for value in values.values()):
            raise ReleaseAcceptanceSpecError("controlled defect challenge has missing identity fields")
        for name in ("baseline_review_hash", "mutated_review_hash", "original_value_hash", "mutated_value_hash"):
            if not _valid_sha256(values[name]):
                raise ReleaseAcceptanceSpecError(
                    f"controlled defect challenge {name} is not SHA-256"
                )
        return cls(**values)


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
        *,
        acceptance_run_id: str = "",
        scenario_id: str = "",
        job_id: str = "",
    ) -> Path:
        target = Path(path).expanduser().resolve()
        previous_raw = b""
        previous_revision = 0
        if target.is_file():
            try:
                previous_raw = target.read_bytes()
                previous_payload = json.loads(previous_raw.decode("utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index is unreadable"
                ) from exc
            if not isinstance(previous_payload, Mapping):
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index is not an object"
                )
            previous_sha = str(previous_payload.get("final_sha") or "").strip()
            if previous_sha and previous_sha != self.final_sha:
                raise ReleaseAcceptanceSpecError(
                    "acceptance evidence index belongs to a different executable SHA"
                )
            raw_revision = previous_payload.get("revision", 0)
            if isinstance(raw_revision, bool) or not isinstance(raw_revision, int) or raw_revision < 0:
                raise ReleaseAcceptanceSpecError(
                    "existing acceptance evidence index revision is invalid"
                )
            previous_revision = raw_revision
        revision = previous_revision + 1
        payload = {
            "schema_version": "release-acceptance-evidence-index-v1",
            "final_sha": self.final_sha,
            "acceptance_run_id": str(acceptance_run_id or ""),
            "scenario_id": str(scenario_id or ""),
            "job_id": str(job_id or ""),
            "revision": revision,
            "previous_revision_hash": (
                hashlib.sha256(previous_raw).hexdigest() if previous_raw else ""
            ),
            "gates": {
                str(gate): self.build_gate(str(gate), list(refs))
                for gate, refs in gates.items()
            },
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")

        def write_atomic(destination: Path) -> None:
            temp = destination.with_name(f".{destination.name}.{os.getpid()}.{revision}.tmp")
            try:
                with temp.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                atomic_replace_with_retry(temp, destination, timeout_seconds=5.0)
            finally:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

        revision_path = target.with_name(f"{target.stem}.revision-{revision}{target.suffix}")
        write_atomic(revision_path)
        write_atomic(target)
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
            "interruption": False,
            "resume": False,
            "duplicate_receipts": 0,
            "reexecuted_completed_call_ids": [],
            "heterogeneity": 0,
            "free_mode_route_only": False,
            "profile_durable": False,
            "defect_injected": False,
            "defect_detected": False,
            "repair_applied": False,
            "revalidated_clean": False,
            "playwright": False,
            "browser_evidence": False,
            "flow_completed": False,
            "trace_archive": False,
            "ocr_actually_used": False,
            "lineage": False,
            "page_identity": False,
            "evidence_artifact": False,
            "stage1_consumed": False,
            "process_count": 0,
            "bounded_wait": False,
            "no_corrupt_json": False,
            "no_lost_update": False,
            "outline_complete": False,
            "docx_complete": False,
            "validation_complete": False,
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
                    test_only_value = row.get("test_only", False)
                    metadata_test_only = (
                        metadata.get("test_only", False)
                        if isinstance(metadata, Mapping)
                        else False
                    )
                    test_only = (
                        test_only_value is True
                        or (
                            isinstance(test_only_value, str)
                            and test_only_value.strip().casefold() == "true"
                        )
                        or metadata_test_only is True
                        or (
                            isinstance(metadata_test_only, str)
                            and metadata_test_only.strip().casefold() == "true"
                        )
                    )
                    malformed_test_only = (
                        not isinstance(test_only_value, (bool, str))
                    ) or (
                        isinstance(test_only_value, str)
                        and test_only_value.strip().casefold() not in {"true", "false", ""}
                    ) or (
                        not isinstance(metadata_test_only, (bool, str))
                    ) or (
                        isinstance(metadata_test_only, str)
                        and metadata_test_only.strip().casefold() not in {"true", "false", ""}
                    )
                    attempted = not malformed_test_only and not test_only and (
                        status == "success"
                        or (isinstance(metadata, Mapping) and bool(metadata.get("transport_config")))
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

    @staticmethod
    def _rows(payload: Any) -> list[Mapping[str, Any]]:
        raw_rows = payload if isinstance(payload, list) else [payload]
        return [row for row in raw_rows if isinstance(row, Mapping)]

    @staticmethod
    def _timestamp(value: Any) -> float | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _string_list(value: Any) -> list[str] | None:
        if not isinstance(value, (list, tuple)):
            return None
        result = [str(item).strip() for item in value]
        if any(not item for item in result):
            return None
        return result

    @staticmethod
    def _semantic_role(node_id: Any) -> str:
        value = str(node_id or "").strip()
        if re.fullmatch(r"candidate_[0-9]+_provider_generation", value):
            return "candidate_provider_generation"
        return value

    def _derive_semantic_facts(
        self,
        gate: str,
        refs: list[DurableEvidenceRefV1],
        payloads: Mapping[str, Any],
        raw_by_ref: Mapping[str, bytes],
        *,
        origin_dir: str | Path | None,
        expected_job_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        by_role: dict[str, list[DurableEvidenceRefV1]] = {}
        for ref in refs:
            by_role.setdefault(ref.role, []).append(ref)

        if gate == "I":
            trace_refs = by_role.get("playwright_trace", [])
            browser_refs = by_role.get("browser_evidence", [])
            if len(trace_refs) != 1 or len(browser_refs) != 1:
                return {}, "Playwright evidence requires one trace archive and one run metadata artifact"
            trace_ref = trace_refs[0]
            trace_path = self._resolve_path(trace_ref.path, origin_dir=origin_dir)
            if trace_path.suffix.casefold() != ".zip":
                return {}, "Playwright trace evidence must be a trace.zip archive"
            try:
                with zipfile.ZipFile(BytesIO(raw_by_ref[trace_ref.ref_id])) as archive:
                    if archive.testzip() is not None:
                        return {}, "Playwright trace archive is corrupt"
                    names = {name.casefold().lstrip("/") for name in archive.namelist()}
            except (KeyError, OSError, ValueError, zipfile.BadZipFile) as exc:
                return {}, f"Playwright trace archive is invalid: {type(exc).__name__}"
            if not any(name.endswith("trace.trace") for name in names):
                return {}, "Playwright trace archive has no trace.trace event stream"
            browser_payload = payloads.get(browser_refs[0].ref_id)
            if not isinstance(browser_payload, Mapping):
                return {}, "Playwright run metadata must be a JSON object"
            if browser_payload.get("artifact_type") != "playwright_run_evidence":
                return {}, "Playwright run metadata artifact type is invalid"
            if browser_payload.get("schema_version") != "playwright-run-evidence-v1":
                return {}, "Playwright run metadata schema is invalid"
            for field_name in ("run_id", "session_id", "url", "resulting_job_id", "trace_sha256"):
                if not str(browser_payload.get(field_name) or "").strip():
                    return {}, f"Playwright run metadata requires {field_name}"
            parsed_url = str(browser_payload["url"]).strip()
            try:
                from urllib.parse import urlsplit

                url = urlsplit(parsed_url)
            except ValueError:
                return {}, "Playwright run metadata URL is invalid"
            if url.scheme != "http" or (url.hostname or "").casefold() not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                return {}, "Playwright run metadata must bind to a localhost URL"
            assertions = browser_payload.get("flow_assertions")
            if not isinstance(assertions, list) or not assertions:
                return {}, "Playwright run metadata requires non-empty flow assertions"
            if any(
                not isinstance(item, Mapping)
                or item.get("passed") is not True
                or not str(item.get("name") or "").strip()
                for item in assertions
            ):
                return {}, "Playwright flow assertions are incomplete or failed"
            console_errors = browser_payload.get("console_errors")
            page_errors = browser_payload.get("page_errors", [])
            if not isinstance(console_errors, list) or not isinstance(page_errors, list):
                return {}, "Playwright error captures must be arrays"
            if console_errors or page_errors:
                return {}, "Playwright evidence contains console or page errors"
            if str(browser_payload.get("trace_sha256")) != trace_ref.sha256:
                return {}, "Playwright run metadata is not bound to the trace archive"
            if expected_job_id and str(browser_payload.get("resulting_job_id")) != expected_job_id:
                return {}, "Playwright run metadata resulting job does not match acceptance job"
            return {
                "playwright": True,
                "browser_evidence": True,
                "trace_archive": True,
                "flow_completed": True,
            }, None

        if gate == "K":
            event_refs = by_role.get("process_events", [])
            lock_refs = by_role.get("lock_state", [])
            if len(event_refs) != 1 or len(lock_refs) != 1:
                return {}, "contention evidence requires one typed process-event log and one lock-state result"
            events = self._rows(payloads.get(event_refs[0].ref_id))
            if not events:
                return {}, "contention process-event log is empty"
            process_identities: set[tuple[str, str]] = set()
            committed_ids: list[str] = []
            revisions: list[int] = []
            for row in events:
                if row.get("artifact_type") != "acceptance_process_event":
                    return {}, "contention process events are not typed acceptance_process_event records"
                if row.get("schema_version") != "process-event-v1":
                    return {}, "contention process-event schema is invalid"
                if not str(row.get("acceptance_run_id") or "").strip() or not str(
                    row.get("scenario_id") or ""
                ).strip():
                    return {}, "contention process events lack run/scenario identity"
                pid = str(row.get("pid") or "").strip()
                creation = str(row.get("process_creation_identity") or "").strip()
                try:
                    pid_value = int(pid)
                except (TypeError, ValueError):
                    pid_value = 0
                if not pid or not creation or pid_value <= 0:
                    return {}, "contention process events lack process creation identity"
                if self._timestamp(row.get("occurred_at")) is None:
                    return {}, "contention process events lack a valid occurred_at timestamp"
                event_name = str(row.get("event") or "").strip()
                if not event_name:
                    return {}, "contention process event name is missing"
                process_identities.add((pid, creation))
                operation_id = str(row.get("operation_id") or "").strip()
                if event_name == "operation_committed":
                    if not operation_id:
                        return {}, "committed contention event lacks operation_id"
                    committed_ids.append(operation_id)
                    raw_revision = row.get("revision")
                    if isinstance(raw_revision, bool) or not isinstance(raw_revision, int):
                        return {}, "committed contention event lacks integer revision"
                    revisions.append(raw_revision)
            lock_payload = payloads.get(lock_refs[0].ref_id)
            if not isinstance(lock_payload, Mapping):
                return {}, "contention lock-state result must be a JSON object"
            if lock_payload.get("artifact_type") != "contention_result":
                return {}, "contention lock-state artifact type is invalid"
            if lock_payload.get("schema_version") != "contention-result-v1":
                return {}, "contention lock-state schema is invalid"
            expected_ids = self._string_list(lock_payload.get("expected_operation_ids"))
            if expected_ids is None or not expected_ids or len(set(expected_ids)) != len(expected_ids):
                return {}, "contention result requires unique expected operation IDs"
            if set(committed_ids) != set(expected_ids) or len(committed_ids) != len(set(committed_ids)):
                return {}, "contention result operation IDs do not prove exactly-once publication"
            if revisions != sorted(set(revisions)) or len(revisions) != len(expected_ids):
                return {}, "contention result revisions are not monotonic and unique"
            acquisitions = lock_payload.get("lock_acquisitions")
            if not isinstance(acquisitions, list) or not acquisitions:
                return {}, "contention result requires lock acquisition timing records"
            for item in acquisitions:
                if not isinstance(item, Mapping):
                    return {}, "contention lock acquisition record is invalid"
                requested = self._timestamp(item.get("requested_at"))
                acquired = self._timestamp(item.get("acquired_at"))
                released = self._timestamp(item.get("released_at"))
                try:
                    timeout = float(str(item.get("timeout_seconds")))
                except (TypeError, ValueError):
                    return {}, "contention lock timeout is invalid"
                if requested is None or acquired is None or released is None or timeout < 0:
                    return {}, "contention lock timing fields are incomplete"
                if acquired < requested or released < acquired or acquired - requested > timeout:
                    return {}, "contention lock wait exceeded its durable timeout"
            final_json_refs = lock_payload.get("final_json_refs")
            if not isinstance(final_json_refs, list) or not final_json_refs:
                return {}, "contention result requires final JSON references"
            for item in final_json_refs:
                if not isinstance(item, Mapping):
                    return {}, "contention final JSON reference is invalid"
                raw_path = str(item.get("path") or "").strip()
                expected_hash = str(item.get("sha256") or "").strip()
                raw_size = item.get("size")
                if not raw_path or not _valid_sha256(expected_hash) or isinstance(raw_size, bool) or not isinstance(raw_size, int):
                    return {}, "contention final JSON reference lacks identity, hash, or size"
                try:
                    final_path = self._resolve_path(raw_path, origin_dir=origin_dir)
                    final_raw = _bounded_read(final_path, max_bytes=self.max_bytes)
                    if len(final_raw) != raw_size or hashlib.sha256(final_raw).hexdigest() != expected_hash:
                        return {}, "contention final JSON reference hash or size mismatch"
                    self._json_payload(final_raw, final_path)
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError, ReleaseAcceptanceSpecError) as exc:
                    return {}, f"contention final JSON is corrupt or unreadable: {type(exc).__name__}"
            budget = lock_payload.get("budget")
            ledger = lock_payload.get("provider_ledger")
            registry = lock_payload.get("registry")
            queue = lock_payload.get("queue")
            if not all(isinstance(item, Mapping) for item in (budget, ledger, registry, queue)):
                return {}, "contention result lacks budget, provider-ledger, Registry, or queue derivations"
            budget_map = cast(Mapping[str, Any], budget)
            ledger_map = cast(Mapping[str, Any], ledger)
            registry_map = cast(Mapping[str, Any], registry)
            queue_map = cast(Mapping[str, Any], queue)
            try:
                limit = int(str(budget_map.get("max_provider_calls_total") or 0))
                used = int(str(budget_map.get("calls_used") or 0))
                reserved = int(str(budget_map.get("calls_reserved") or 0))
            except (TypeError, ValueError):
                return {}, "contention budget derivation is invalid"
            if limit > 0 and (used < 0 or reserved < 0 or used + reserved > limit):
                return {}, "contention budget derivation proves an overshoot"
            if ledger_map.get("duplicate_receipt_ids") != [] or ledger_map.get("conflicts") != []:
                return {}, "contention provider ledger derivation contains duplicates or conflicts"
            if int(str(registry_map.get("lost_updates") or 0)) != 0 or queue_map.get("duplicate_operation_ids") != []:
                return {}, "contention Registry or queue derivation contains a lost update"
            return {
                "process_count": len(process_identities),
                "bounded_wait": True,
                "no_corrupt_json": True,
                "no_lost_update": True,
            }, None

        if gate == "E":
            interruption_refs = by_role.get("interruption_event", [])
            resume_refs = by_role.get("resume_event", [])
            event_refs = by_role.get("process_events", [])
            if len(interruption_refs) != 1 or len(resume_refs) != 1 or len(event_refs) != 1:
                return {}, "resume evidence requires one typed interruption, resume, and process-event artifact"
            interruption = payloads.get(interruption_refs[0].ref_id)
            resume = payloads.get(resume_refs[0].ref_id)
            if not isinstance(interruption, Mapping) or not isinstance(resume, Mapping):
                return {}, "interruption and resume evidence must be JSON objects"
            if interruption.get("artifact_type") != "process_interruption_event" or interruption.get("schema_version") != "process-interruption-event-v1":
                return {}, "interruption evidence type or schema is invalid"
            if resume.get("artifact_type") != "process_resume_event" or resume.get("schema_version") != "process-resume-event-v1":
                return {}, "resume evidence type or schema is invalid"
            interruption_id = str(interruption.get("event_id") or "").strip()
            interruption_job = str(interruption.get("job_id") or "").strip()
            interruption_attempt = str(interruption.get("attempt_id") or "").strip()
            if not interruption_id or not interruption_job or not interruption_attempt:
                return {}, "interruption evidence lacks event, job, or attempt identity"
            if expected_job_id and interruption_job != expected_job_id:
                return {}, "interruption evidence belongs to a different job"
            for name in ("pid", "process_creation_identity", "interrupted_at", "interruption_method", "last_durable_stage"):
                if not str(interruption.get(name) or "").strip():
                    return {}, f"interruption evidence requires {name}"
            if self._timestamp(interruption.get("started_at")) is None or self._timestamp(interruption.get("interrupted_at")) is None:
                return {}, "interruption evidence timestamps are invalid"
            if interruption.get("exit_code") is None:
                return {}, "interruption evidence requires child exit_code"
            if str(resume.get("interruption_event_id") or "") != interruption_id:
                return {}, "resume evidence does not reference the interruption event"
            if str(resume.get("interruption_event_sha256") or "") != interruption_refs[0].sha256:
                return {}, "resume evidence is not hash-bound to the interruption event"
            if str(resume.get("previous_attempt_id") or "") != interruption_attempt:
                return {}, "resume evidence previous attempt does not match interruption"
            new_attempt = str(resume.get("new_attempt_id") or "").strip()
            if not new_attempt or new_attempt == interruption_attempt or not str(resume.get("new_pid") or "").strip():
                return {}, "resume evidence lacks a distinct new attempt and process"
            event_rows = self._rows(payloads.get(event_refs[0].ref_id))
            if not event_rows or any(
                row.get("artifact_type") != "acceptance_process_event"
                or row.get("schema_version") != "process-event-v1"
                for row in event_rows
            ):
                return {}, "resume process events are not typed acceptance records"
            snapshots: dict[str, Mapping[str, Any]] = {}
            for row in event_rows:
                if str(row.get("event") or "") != "ledger_snapshot":
                    continue
                name = str(row.get("snapshot_name") or "").strip()
                if name in {"before", "at_interruption", "after_resume"}:
                    snapshots[name] = row
            if set(snapshots) != {"before", "at_interruption", "after_resume"}:
                return {}, "resume evidence requires before, interruption, and after-resume ledger snapshots"
            receipt_refs = by_role.get("provider_receipt_ledger", [])
            receipts: list[Mapping[str, Any]] = []
            for ref in receipt_refs:
                receipts.extend(self._rows(payloads.get(ref.ref_id)))
            receipt_ids = [str(row.get("receipt_id") or "") for row in receipts]
            if any(not value for value in receipt_ids):
                return {}, "resume provider ledger contains a receipt without receipt_id"
            duplicate_count = len(receipt_ids) - len(set(receipt_ids))
            before_ids = set(self._string_list(snapshots["before"].get("receipt_ids")) or [])
            interruption_ids = set(self._string_list(snapshots["at_interruption"].get("receipt_ids")) or [])
            after_ids = set(self._string_list(snapshots["after_resume"].get("receipt_ids")) or [])
            if not before_ids.issubset(interruption_ids) or not interruption_ids.issubset(after_ids) or after_ids != set(receipt_ids):
                return {}, "resume ledger snapshots do not form a durable monotonic receipt lineage"
            before_completed = set(self._string_list(snapshots["before"].get("completed_call_ids")) or [])
            new_after = after_ids - before_ids
            reexecuted = {
                str(row.get("call_id") or "")
                for row in receipts
                if str(row.get("receipt_id") or "") in new_after
                and str(row.get("call_id") or "") in before_completed
            }
            try:
                actual_calls = sum(int(row.get("attempts") or 0) for row in receipts)
                actual_output = sum(int(row.get("output_tokens") or 0) for row in receipts)
                actual_retries = sum(max(0, int(row.get("attempts") or 0) - 1) for row in receipts)
                after_calls = int(str(snapshots["after_resume"].get("provider_calls")))
                after_output = int(str(snapshots["after_resume"].get("output_tokens")))
                after_retries = int(str(snapshots["after_resume"].get("retry_attempts")))
            except (TypeError, ValueError):
                return {}, "resume ledger snapshot usage counters are invalid"
            if (actual_calls, actual_output, actual_retries) != (after_calls, after_output, after_retries):
                return {}, "resume ledger snapshot usage does not match the reopened provider ledger"
            return {
                "interruption": True,
                "resume": True,
                "duplicate_receipts": duplicate_count,
                "reexecuted_completed_call_ids": sorted(reexecuted),
                "provider_call_ledger_delta": {
                    "receipt_ids_before": sorted(before_ids),
                    "receipt_ids_after_resume": sorted(after_ids),
                    "new_receipts_after_resume": sorted(new_after),
                    "reexecuted_completed_call_ids": sorted(reexecuted),
                    "provider_calls_delta": after_calls - int(str(snapshots["before"].get("provider_calls") or 0)),
                    "output_tokens_delta": after_output - int(str(snapshots["before"].get("output_tokens") or 0)),
                    "retry_delta": after_retries - int(str(snapshots["before"].get("retry_attempts") or 0)),
                },
            }, None

        if gate == "F":
            plan_refs = by_role.get("outline_provider_call_plan", [])
            if len(plan_refs) != 1:
                return {}, "Outline role closure requires one authoritative provider call plan"
            plan_payload = payloads.get(plan_refs[0].ref_id)
            if not isinstance(plan_payload, Mapping):
                return {}, "Outline provider call plan is not a JSON object"
            route_plan = plan_payload.get("reachable_provider_route_plan")
            if not isinstance(route_plan, Mapping):
                return {}, "Outline provider call plan lacks ReachableProviderRoutePlan"
            raw_routes = route_plan.get("routes")
            if not isinstance(raw_routes, list) or not raw_routes:
                return {}, "Outline provider route plan has no routes"
            expected_routes: dict[str, Mapping[str, Any]] = {}
            for route in raw_routes:
                if not isinstance(route, Mapping) or route.get("enabled") is not True:
                    continue
                role = str(route.get("semantic_role") or "").strip()
                if not role or route.get("resolved") is not True:
                    return {}, "Outline route plan contains an enabled unresolved route"
                expected_routes[role] = route
            if not expected_routes:
                return {}, "Outline route plan has no enabled resolved semantic roles"
            receipts: list[Mapping[str, Any]] = []
            for ref in by_role.get("provider_receipt_ledger", []):
                receipts.extend(self._rows(payloads.get(ref.ref_id)))
            executed: dict[str, list[Mapping[str, Any]]] = {}
            for row in receipts:
                role = self._semantic_role(row.get("node_id") or row.get("route"))
                if role:
                    executed.setdefault(role, []).append(row)
            mismatches: list[str] = []
            for role, route in expected_routes.items():
                candidates = executed.get(role, [])
                if not candidates:
                    mismatches.append(f"missing:{role}")
                    continue
                expected_provider = str(route.get("provider_family") or "").strip()
                expected_model = str(route.get("model") or "").strip()
                expected_endpoint = str(route.get("endpoint_type") or "").strip()
                expected_host = str(route.get("api_base_host") or "").strip().casefold()
                expected_section = str(route.get("section") or "").strip()
                for row in candidates:
                    raw_metadata = row.get("metadata")
                    metadata = cast(Mapping[str, Any], raw_metadata) if isinstance(raw_metadata, Mapping) else {}
                    raw_transport = metadata.get("transport_config")
                    transport = cast(Mapping[str, Any], raw_transport) if isinstance(raw_transport, Mapping) else {}
                    actual_host = str(transport.get("api_base") or row.get("endpoint") or "").strip()
                    try:
                        from urllib.parse import urlsplit

                        actual_host = (urlsplit(actual_host).hostname or "").casefold()
                    except ValueError:
                        actual_host = ""
                    if (
                        str(row.get("provider") or "") != expected_provider
                        or str(row.get("model") or "") != expected_model
                        or str(row.get("endpoint_type") or "") != expected_endpoint
                        or (expected_host and actual_host != expected_host)
                        or str(metadata.get("config_section") or "") != expected_section
                        or not str(metadata.get("route_fingerprint") or "").strip()
                    ):
                        mismatches.append(f"identity:{role}")
            if mismatches:
                return {}, "Outline semantic-role/provider closure failed: " + ", ".join(sorted(set(mismatches)))
            actual_roles = sorted(set(expected_routes).intersection(executed))
            candidate_count = len(executed.get("candidate_provider_generation", []))
            return {
                "route_plan_present": True,
                "semantic_roles": actual_roles,
                "executed_semantic_roles": actual_roles,
                "required_semantic_roles": sorted(expected_routes),
                "candidate_count": candidate_count,
            }, None

        if gate == "D":
            source_refs = by_role.get("source_pdf", [])
            profile_refs = by_role.get("modality_profile", [])
            source_hashes = {ref.sha256 for ref in source_refs}
            if len(source_hashes) < 3 or len(profile_refs) < 3:
                return {}, "heterogeneous gate requires three source PDFs and three derived modality profiles"
            profiles: list[DocumentModalityProfileV1] = []
            for ref in profile_refs:
                payload = payloads.get(ref.ref_id)
                if not isinstance(payload, Mapping):
                    return {}, "document modality profile is not a JSON object"
                try:
                    profile = DocumentModalityProfileV1.from_mapping(payload)
                except ReleaseAcceptanceSpecError as exc:
                    return {}, str(exc)
                if profile.source_pdf_sha256 not in source_hashes:
                    return {}, "document modality profile is not bound to a source PDF"
                profiles.append(profile)
            modalities = {profile.derived_modality for profile in profiles}
            if len(modalities) < 3:
                return {}, "heterogeneous gate requires text-heavy, visual/table-heavy, and OCR/scanned derived profiles"
            return {
                "source_count": len(source_hashes),
                "heterogeneity": len(modalities),
                "derived_modalities": sorted(modalities),
                "modality_profiles": [profile.source_pdf_sha256 for profile in profiles],
            }, None

        if gate == "H":
            challenge_refs = by_role.get("defect_artifact", [])
            validation_refs = by_role.get("validation_artifact", [])
            repair_refs = by_role.get("repair_artifact", [])
            if len(challenge_refs) != 1 or not validation_refs or len(repair_refs) != 1:
                return {}, "Validator challenge requires one challenge, validation, and repair artifact"
            challenge_payload = payloads.get(challenge_refs[0].ref_id)
            if not isinstance(challenge_payload, Mapping):
                return {}, "controlled Validator challenge is not a JSON object"
            try:
                challenge = ControlledDefectChallengeV1.from_mapping(challenge_payload)
            except ReleaseAcceptanceSpecError as exc:
                return {}, str(exc)
            try:
                mutated_path = self._resolve_path(
                    challenge.mutated_review_path,
                    origin_dir=origin_dir,
                )
                mutated_raw = _bounded_read(mutated_path, max_bytes=self.max_bytes)
            except (OSError, ReleaseAcceptanceSpecError) as exc:
                return {}, f"controlled defect copy is unreadable: {type(exc).__name__}"
            if hashlib.sha256(mutated_raw).hexdigest() != challenge.mutated_review_hash:
                return {}, "controlled defect copy hash does not match the challenge"
            if challenge.mutated_review_hash == challenge.baseline_review_hash:
                return {}, "controlled defect challenge did not mutate a copy"
            detected_ids: set[str] = set()
            detected = False
            revalidated_clean = False
            for ref in validation_refs:
                payload = payloads.get(ref.ref_id)
                for row in self._rows(payload):
                    if str(row.get("challenge_id") or "") != challenge.challenge_id:
                        continue
                    findings = row.get("detected_findings")
                    if isinstance(findings, list):
                        for finding in findings:
                            if not isinstance(finding, Mapping):
                                continue
                            if str(finding.get("mutation_locator") or "") == challenge.mutation_locator:
                                finding_id = str(finding.get("finding_id") or "").strip()
                                if finding_id:
                                    detected_ids.add(finding_id)
                                detected = detected or bool(finding_id)
                    if row.get("phase") == "revalidation" and row.get("status") in {"clean", "succeeded", "passed"}:
                        remaining = row.get("remaining_challenge_findings")
                        resolved = self._string_list(row.get("resolved_finding_ids"))
                        revalidated_clean = isinstance(remaining, list) and not remaining and bool(
                            resolved and detected_ids.intersection(resolved)
                        )
            repair_payload = payloads.get(repair_refs[0].ref_id)
            repair_rows = self._rows(repair_payload)
            repair_applied = False
            for row in repair_rows:
                if str(row.get("challenge_id") or "") != challenge.challenge_id:
                    continue
                if (
                    str(row.get("before_hash") or "") == challenge.mutated_review_hash
                    and _valid_sha256(row.get("after_hash"))
                    and str(row.get("after_hash")) != challenge.mutated_review_hash
                    and str(row.get("changed_locator") or "") == challenge.mutation_locator
                    and str(row.get("status") or "").casefold() in {"applied", "completed", "promoted"}
                ):
                    repair_applied = True
            if not detected:
                return {}, "Validator did not explicitly detect the challenged mutation"
            if not repair_applied:
                return {}, "Validator repair artifact is not bound to the challenged mutation"
            if not revalidated_clean:
                return {}, "Validator revalidation did not prove the challenged finding was resolved"
            return {
                "defect_injected": True,
                "defect_detected": True,
                "repair_applied": True,
                "revalidated_clean": True,
                "challenge_id": challenge.challenge_id,
                "detected_finding_ids": sorted(detected_ids),
            }, None

        if gate == "J":
            source_refs = by_role.get("source_pdf", [])
            diagnostics_refs = by_role.get("ocr_diagnostics", [])
            artifact_refs = by_role.get("ocr_artifact", [])
            canonical_refs = by_role.get("canonical_stage1", [])
            registry_refs = by_role.get("registry", [])
            if len(source_refs) != 1 or len(diagnostics_refs) != 1 or len(artifact_refs) != 1 or len(canonical_refs) != 1 or len(registry_refs) != 1:
                return {}, "OCR gate requires one source, diagnostics, OCR artifact, canonical Stage 1, and Registry reference"
            source_hash = source_refs[0].sha256
            diagnostics = payloads.get(diagnostics_refs[0].ref_id)
            ocr_artifact = payloads.get(artifact_refs[0].ref_id)
            canonical = payloads.get(canonical_refs[0].ref_id)
            registry = payloads.get(registry_refs[0].ref_id)
            if not all(isinstance(item, Mapping) for item in (diagnostics, ocr_artifact, canonical, registry)):
                return {}, "OCR lineage artifacts must be JSON objects"
            diagnostics = cast(Mapping[str, Any], diagnostics)
            ocr_artifact = cast(Mapping[str, Any], ocr_artifact)
            canonical = cast(Mapping[str, Any], canonical)
            registry = cast(Mapping[str, Any], registry)
            if diagnostics.get("artifact_type") != "ocr_diagnostics" or diagnostics.get("schema_version") != "ocr-diagnostics-v1":
                return {}, "OCR diagnostics type or schema is invalid"
            if str(diagnostics.get("source_pdf_sha256") or "") != source_hash:
                return {}, "OCR diagnostics source hash does not match the source PDF"
            page_numbers = self._string_list(diagnostics.get("page_numbers"))
            if not page_numbers or int(diagnostics.get("ocr_page_count") or 0) != len(page_numbers):
                return {}, "OCR diagnostics page identity is incomplete"
            if not str(diagnostics.get("ocr_engine") or "").strip() or not str(diagnostics.get("ocr_engine_version") or "").strip():
                return {}, "OCR diagnostics engine identity is incomplete"
            if ocr_artifact.get("artifact_type") != "ocr_artifact" or ocr_artifact.get("schema_version") != "ocr-artifact-v1":
                return {}, "OCR output artifact type or schema is invalid"
            if str(ocr_artifact.get("source_pdf_sha256") or "") != source_hash:
                return {}, "OCR output artifact source hash does not match the source PDF"
            if str(ocr_artifact.get("diagnostics_sha256") or "") != diagnostics_refs[0].sha256:
                return {}, "OCR output artifact is not bound to diagnostics"
            canonical_payload = canonical.get("payload") if isinstance(canonical.get("payload"), Mapping) else canonical
            lineage = canonical_payload.get("ocr_lineage") if isinstance(canonical_payload, Mapping) else None
            if not isinstance(lineage, Mapping):
                return {}, "canonical Stage 1 artifact lacks OCR lineage"
            if (
                str(lineage.get("source_pdf_sha256") or "") != source_hash
                or str(lineage.get("ocr_artifact_sha256") or "") != artifact_refs[0].sha256
                or not str(lineage.get("stage1_input_sha256") or "").strip()
            ):
                return {}, "canonical Stage 1 OCR lineage is incomplete or mismatched"
            dependency_ids = self._string_list(lineage.get("registry_dependency_artifact_ids"))
            records = registry.get("artifacts")
            if not dependency_ids or not isinstance(records, list):
                return {}, "OCR lineage lacks Registry dependency identities"
            registered_ids = {
                str(item.get("artifact_id") or "")
                for item in records
                if isinstance(item, Mapping)
            }
            if not set(dependency_ids).issubset(registered_ids):
                return {}, "OCR lineage references artifacts absent from the Registry"
            return {
                "ocr_actually_used": True,
                "page_identity": True,
                "evidence_artifact": True,
                "stage1_consumed": True,
                "lineage": True,
                "ocr_page_numbers": page_numbers,
            }, None

        if gate == "Q":
            source_refs = by_role.get("source_pdf", [])
            canonical_refs = by_role.get("canonical_stage1", [])
            if len({ref.sha256 for ref in source_refs}) != 15 or not canonical_refs:
                return {}, "15-paper gate requires exactly fifteen distinct source PDFs and canonical Stage 1 evidence"
            source_hashes = {ref.sha256 for ref in source_refs}
            paper_keys: set[str] = set()
            paper_hashes: set[str] = set()
            for ref in canonical_refs:
                payload = payloads.get(ref.ref_id)
                if not isinstance(payload, Mapping):
                    return {}, "canonical Stage 1 aggregate is not a JSON object"
                candidate_raw = payload.get("payload")
                candidate = cast(Mapping[str, Any], candidate_raw) if isinstance(candidate_raw, Mapping) else payload
                summaries = candidate.get("summaries") if isinstance(candidate, Mapping) else None
                if isinstance(summaries, list):
                    items = summaries
                else:
                    items = [candidate]
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    key = str(
                        item.get("canonical_paper_key")
                        or item.get("paper_key")
                        or item.get("paper_id")
                        or item.get("source_pdf_sha256")
                        or ""
                    ).strip()
                    source_hash = str(item.get("source_pdf_sha256") or "").strip()
                    if not key:
                        continue
                    paper_keys.add(key)
                    if source_hash:
                        if source_hash not in source_hashes:
                            return {}, "canonical Stage 1 paper identity is not bound to the authoritative corpus"
                        paper_hashes.add(source_hash)
            if len(paper_keys) != 15 or paper_hashes != source_hashes:
                return {}, "canonical Stage 1 aggregate does not prove fifteen distinct paper identities"
            outline_complete = False
            for ref in by_role.get("outline_terminal", []):
                for row in self._rows(payloads.get(ref.ref_id)):
                    outline_complete = outline_complete or str(row.get("status") or row.get("closure_status") or "").casefold() in {"complete", "completed", "succeeded"}
            docx_complete = False
            for ref in by_role.get("review_docx", []):
                raw = raw_by_ref.get(ref.ref_id, b"")
                try:
                    with zipfile.ZipFile(BytesIO(raw)) as archive:
                        document_xml = archive.read("word/document.xml").decode("utf-8", errors="strict")
                        docx_complete = (
                            "<w:body" in document_xml
                            and "reference" in document_xml.casefold()
                            and not any(marker in document_xml.casefold() for marker in ("placeholder", "your_", "todo"))
                        )
                except (KeyError, UnicodeError, zipfile.BadZipFile):
                    docx_complete = False
            citation_complete = False
            for ref in by_role.get("citation_manifest", []):
                payload = payloads.get(ref.ref_id)
                if isinstance(payload, Mapping):
                    entries = payload.get("citations") or payload.get("references") or payload.get("items")
                    citation_complete = isinstance(entries, list) and bool(entries)
            validation_complete = False
            for ref in by_role.get("validation_artifact", []):
                for row in self._rows(payloads.get(ref.ref_id)):
                    validation_complete = validation_complete or str(row.get("status") or row.get("validation_status") or "").casefold() in {"clean", "completed", "succeeded", "passed"}
            if not outline_complete or not docx_complete or not citation_complete or not validation_complete:
                return {}, "15-paper gate lacks complete Outline, DOCX, citation, or validation semantics"
            return {
                "source_count": 15,
                "corpus_count": 15,
                "canonical_stage1_count": 15,
                "paper_identity_count": len(paper_keys),
                "paper_keys": sorted(paper_keys),
                "outline_complete": True,
                "docx_complete": True,
                "validation_complete": True,
            }, None

        return {}, None

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
        raw_by_ref: dict[str, bytes] = {}
        try:
            for ref in refs:
                path = self._resolve_path(ref.path, origin_dir=origin_dir)
                raw = _bounded_read(path, max_bytes=self.max_bytes)
                raw_by_ref[ref.ref_id] = raw
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
                if ref.role == "provider_receipt_ledger":
                    if path.suffix.casefold() != ".jsonl":
                        return {
                            "status": "FAIL",
                            "reason": f"provider receipt evidence must be JSONL: {ref.ref_id}",
                            "contract": contract,
                        }
                    try:
                        receipts = ProviderRuntimeLedger(path).list_acceptance_receipts(
                            expected_job_id=expected_job_id or ref.job_id,
                        )
                    except (OSError, ValueError, ProviderRuntimeContractError) as exc:
                        return {
                            "status": "FAIL",
                            "reason": f"malformed provider receipt evidence: {exc}",
                            "contract": contract,
                        }
                    payloads[ref.ref_id] = [receipt.to_dict() for receipt in receipts]
                    continue
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
        semantic_facts, semantic_error = self._derive_semantic_facts(
            str(gate),
            refs,
            payloads,
            raw_by_ref,
            origin_dir=origin_dir,
            expected_job_id=expected_job_id,
        )
        if semantic_error:
            return {
                "status": "FAIL",
                "reason": semantic_error,
                "derived_facts": self._derive_facts(str(gate), refs, payloads),
                "contract": contract,
            }
        facts = self._derive_facts(str(gate), refs, payloads)
        facts.update(semantic_facts)
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
            "I": ("playwright", "browser_evidence", "trace_archive", "flow_completed"),
            "J": ("ocr_actually_used", "page_identity", "evidence_artifact", "stage1_consumed", "lineage"),
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
        if gate == "E" and facts.get("reexecuted_completed_call_ids"):
            return {
                "status": "FAIL",
                "reason": "resume gate re-executed a completed provider call",
                "derived_facts": facts,
                "contract": contract,
            }
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
    "AcceptanceExecutionContextV1",
    "GATE_CONTRACTS",
    "AcceptanceRunStateV1",
    "ControlledDefectChallengeV1",
    "DocumentModalityProfileV1",
    "DurableEvidenceRefV1",
    "GateEvidenceProducer",
    "GateEvidenceVerifier",
    "ReleaseAcceptanceBudget",
    "ReleaseAcceptanceSpec",
    "ReleaseAcceptanceSpecError",
    "gate_contract",
    "validate_gate_evidence",
]

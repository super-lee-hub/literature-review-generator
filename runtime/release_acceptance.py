"""Typed release-acceptance contracts and evidence gates.

This module deliberately does not manufacture evidence.  A successful public
runtime job is only one input to a gate; each specialized gate must carry its
own final-SHA-bound evidence and its own facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from runtime.provider_runtime import ProviderAggregateBudgetV1


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
            third_party_acknowledged=acknowledged,
            third_party_hosts=tuple(item.strip() for item in raw_hosts if item.strip()),
            gates=tuple(item.strip() for item in raw_gates if item.strip()),
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


def _positive_int_fact(evidence: Mapping[str, Any], name: str) -> bool:
    value = evidence.get(name)
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_gate_evidence(
    gate: str,
    evidence: Mapping[str, Any] | None,
    *,
    expected_final_sha: str = "",
) -> dict[str, Any]:
    """Validate one gate's facts without inferring them from job completion."""

    contract = gate_contract(gate)
    if not isinstance(evidence, Mapping):
        return {
            "status": "NOT_VERIFIED",
            "reason": "gate evidence is missing",
            "contract": contract,
        }
    evidence_sha = str(evidence.get("final_sha") or "").strip()
    if not evidence_sha:
        return {
            "status": "NOT_VERIFIED",
            "reason": "gate evidence is not bound to a final checkout SHA",
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
    declared_status = str(evidence.get("status") or "").strip().upper()
    if declared_status in {"BLOCKED", "BLOCKED_CREDENTIAL", "BLOCKED_CORPUS", "BLOCKED_TRUST_POLICY", "NOT_VERIFIED", "FAIL"}:
        return {"status": declared_status, "reason": str(evidence.get("reason") or "evidence declared non-pass"), "contract": contract}
    missing = [field for field in contract["evidence_required"] if field not in evidence]
    if missing:
        return {
            "status": "NOT_VERIFIED",
            "reason": "gate evidence is missing required facts: " + ", ".join(missing),
            "contract": contract,
        }
    if contract["required_live"] and not _positive_int_fact(evidence, "actual_transport_calls"):
        return {
            "status": "FAIL",
            "reason": "a live provider gate requires actual_transport_calls > 0",
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
    false_fields = [
        field
        for field in truthy_fields.get(gate, ())
        if evidence.get(field) is not True
    ]
    if false_fields:
        return {
            "status": "FAIL",
            "reason": "gate evidence did not prove required facts: " + ", ".join(false_fields),
            "contract": contract,
        }
    if gate == "D":
        heterogeneity = evidence.get("heterogeneity")
        if not isinstance(heterogeneity, (list, tuple, Mapping)) or len(heterogeneity) < 3:
            return {"status": "FAIL", "reason": "heterogeneous gate requires three distinct modality records", "contract": contract}
    if gate == "E":
        delta = evidence.get("provider_call_ledger_delta")
        if not isinstance(delta, Mapping):
            return {"status": "FAIL", "reason": "resume gate requires a structured provider ledger delta", "contract": contract}
        duplicate_receipts = evidence.get("duplicate_receipts")
        if duplicate_receipts not in (0, False, [], ()):
            return {"status": "FAIL", "reason": "resume gate recorded duplicate receipts", "contract": contract}
    semantic_roles = evidence.get("semantic_roles")
    if gate == "F" and (
        not isinstance(semantic_roles, (list, tuple, Mapping))
        or len(semantic_roles) == 0
        or not _positive_int_fact(evidence, "candidate_count")
    ):
        return {"status": "FAIL", "reason": "Outline gate lacks semantic-role and candidate evidence", "contract": contract}
    if gate == "K" and int(evidence.get("process_count") or 0) < 2:
        return {"status": "FAIL", "reason": "contention gate requires at least two real processes", "contract": contract}
    if gate == "C" and (
        not _positive_int_fact(evidence, "source_count")
        or int(evidence.get("canonical_stage1_count") or 0)
        != int(evidence.get("source_count") or 0)
    ):
        return {"status": "FAIL", "reason": "one-paper source and canonical Stage 1 counts are not a positive matching pair", "contract": contract}
    if gate == "D" and int(evidence.get("source_count") or 0) < 3:
        return {"status": "FAIL", "reason": "heterogeneous gate requires at least three real papers", "contract": contract}
    if gate == "Q" and (
        int(evidence.get("corpus_count") or 0) != 15
        or int(evidence.get("stage1_canonical_count") or 0) != 15
    ):
        return {"status": "FAIL", "reason": "F1 gate requires corpus_count=15 and stage1_canonical_count=15", "contract": contract}
    return {"status": "PASS", "reason": "all gate-specific evidence facts validated", "contract": contract, "evidence": dict(evidence)}


__all__ = [
    "GATE_CONTRACTS",
    "ReleaseAcceptanceBudget",
    "ReleaseAcceptanceSpec",
    "ReleaseAcceptanceSpecError",
    "gate_contract",
    "validate_gate_evidence",
]

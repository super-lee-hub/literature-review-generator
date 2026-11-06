"""Finite primary-candidate semantic-repair admission contract.

This module is shared by the initial projection and the Outline executor. It
only bounds the primary candidate scope; stability scopes need their own plan.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, MutableSet, Sequence
from dataclasses import dataclass
from typing import Any

from runtime.provider_context import ProviderContextProfile
from runtime.provider_runtime import hash_json
from runtime.stage_planning import (
    ProviderExposureCardinalityBasisV1,
    ProviderRequestPlanRowV1,
    StagePlanError,
    UnplannedProviderExposureV1,
    build_provider_request_plan_row_v1,
)

MAX_OUTLINE_CANDIDATE_COUNT = 12
OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION = "outline-candidate-repair-plan/v1"
SEMANTIC_REPAIR_TASK_V1 = "semantic_repair_of_outline_candidate"
SEMANTIC_REPAIR_PREDICATE_V1 = "candidate_contract_validation_failed"
SEMANTIC_REPAIR_SCOPE_V1 = "primary"
SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1 = "outline-semantic-repair/v1"
SEMANTIC_REPAIR_REQUEST_BUILDER_ID = (
    "outline.v3_executor.OutlineV3Executor._semantic_repair_candidate"
)
SEMANTIC_REPAIR_SOURCE_BUILDER = (
    "outline.v3_executor.OutlineV3Executor._semantic_repair_candidate"
)

SEMANTIC_REPAIR_RULES_V1: tuple[str, ...] = (
    "Repair ONLY the structure of the candidate sections.",
    "Remove or replace every section paper_key that is not in allowed_paper_ids.",
    "Remove or replace every section relation_id that is not in allowed_relation_ids.",
    "Do not introduce new papers, new relations, new citations, or new facts.",
    "Do not invent citation identities; do not attribute evidence to any work outside the provided evidence corpus.",
    "If a section has no in-corpus evidence left after removal, retain its identity and return needs_manual_review; never invent a replacement fact or silently delete the section.",
    "Do not add sections beyond those in original_provider_output and do not increase the total number of planned claims.",
    "Return exactly the same number of sections in exactly the same order as original_provider_output.",
    "Copy every original section_id verbatim; never create, delete, duplicate, or rename a section_id.",
    "Every claim that names a paper alias must be supported by a paper_key in that same section; remove the claim if its paper is not assigned there.",
    "Do not write cross-paper limitation or gap aggregations unless each named paper explicitly supports the same limitation; prefer separate paper-specific claims or remove the aggregation.",
    "Ensure each section goal accurately covers every remaining claim; rewrite a goal to a neutral evidence-bound purpose when the original is narrower than the claims.",
    "Keep candidate_id unchanged and return the same top-level shape.",
)

SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1: dict[str, str] = {
    "candidate_id": "string; echo the candidate_id verbatim",
    "sections": (
        "non-empty array; each section object has section_id, title, "
        "paper_keys (subset of allowed_paper_ids), relation_ids (subset "
        "of allowed_relation_ids), claims (non-empty) and rationale"
    ),
    "needs_manual_review": "array of section_ids that cannot be repaired without a new semantic decision; empty when none",
}


def _require_sha256(value: str, label: str) -> str:
    text = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        raise StagePlanError(f"candidate repair plan requires a verified {label} SHA-256")
    return text


def _route_identity(route: Any) -> tuple[str, ...]:
    raw = getattr(route, "binding_identity", None) or getattr(route, "identity", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise StagePlanError("candidate repair plan requires a typed reachable route identity")
    identity = tuple(str(item).strip() for item in raw)
    if len(identity) < 3 or any(not item for item in identity[:3]):
        raise StagePlanError("candidate repair route identity is incomplete")
    return identity


def _profile_contract_payload(
    *,
    provider: str,
    model: str,
    endpoint_type: str,
    model_context_limit: int,
    verified_context_limit: int,
    input_budget: int,
    max_output_tokens: int,
    reasoning_reserve: int,
    safety_margin: int,
    tokenizer_strategy: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "endpoint_type": endpoint_type,
        "model_context_limit": model_context_limit,
        "verified_context_limit": verified_context_limit,
        "input_budget": input_budget,
        "max_output_tokens": max_output_tokens,
        "reasoning_reserve": reasoning_reserve,
        "safety_margin": safety_margin,
        "tokenizer_strategy": tokenizer_strategy,
    }


def _predicate_contract_hash(
    *,
    scope: str,
    task: str,
    predicate: str,
    repair_rules: Sequence[str],
    output_schema_sha256: str,
) -> str:
    return hash_json(
        {
            "scope": scope,
            "task": task,
            "predicate_version": predicate,
            "repair_rules": list(repair_rules),
            "output_schema_sha256": output_schema_sha256,
        }
    )


def _candidate_repair_contract_payload(
    *,
    plan_version: str,
    maximum_candidate_count: int,
    scope: str,
    candidate_count: int,
    semantic_repair_enabled: bool,
    request_builder_id: str,
    task: str,
    predicate: str,
    output_schema_version: str,
    repair_rules_sha256: str,
    output_schema_sha256: str,
    predicate_sha256: str,
    route_identity: Sequence[str],
    route_config_fingerprint_sha256: str,
    profile: Mapping[str, Any],
    effective_input_cap: int,
    retry_attempts_per_call_upper_bound: int,
    wall_seconds_per_call_upper_bound: float,
    config_source_id: str,
    config_source_sha256: str,
    runtime_spec_sha256: str,
    canonical_source_authority_id: str,
    canonical_source_authority_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": plan_version,
        "maximum_candidate_count": maximum_candidate_count,
        "scope": scope,
        "candidate_count": candidate_count,
        "candidate_ids": [f"candidate_{index}" for index in range(1, candidate_count + 1)],
        "semantic_repair_enabled": semantic_repair_enabled,
        "repair_attempts_per_candidate": 1,
        "request_builder_id": request_builder_id,
        "task": task,
        "conditional_on": predicate,
        "output_schema_version": output_schema_version,
        "repair_rules_sha256": repair_rules_sha256,
        "predicate_sha256": predicate_sha256,
        "output_schema_sha256": output_schema_sha256,
        "route_identity": list(route_identity),
        "route_config_fingerprint_sha256": route_config_fingerprint_sha256,
        "profile": dict(profile),
        "effective_input_cap": effective_input_cap,
        "retry_attempts_per_call_upper_bound": retry_attempts_per_call_upper_bound,
        "wall_seconds_per_call_upper_bound": float(wall_seconds_per_call_upper_bound),
        "config_source_id": config_source_id,
        "config_source_sha256": config_source_sha256,
        "runtime_spec_sha256": runtime_spec_sha256,
        "canonical_source_authority_id": canonical_source_authority_id,
        "canonical_source_authority_sha256": canonical_source_authority_sha256,
    }


@dataclass(frozen=True)
class OutlineCandidateRepairPlanV1:
    """Hash-bound upper envelope and admission rule for primary repairs."""

    plan_version: str
    maximum_candidate_count: int
    candidate_count: int
    semantic_repair_enabled: bool
    scope: str
    request_builder_id: str
    route_identity: tuple[str, ...]
    route_config_fingerprint_sha256: str
    provider: str
    model: str
    endpoint_type: str
    model_context_limit: int
    verified_context_limit: int
    profile_input_budget: int
    effective_input_cap: int
    output_tokens_per_call: int
    reasoning_tokens_per_call: int
    safety_margin_tokens_per_call: int
    retry_attempts_per_call_upper_bound: int
    wall_seconds_per_call_upper_bound: float
    config_source_id: str
    config_source_sha256: str
    runtime_spec_sha256: str
    canonical_source_authority_id: str
    canonical_source_authority_sha256: str
    profile_tokenizer_strategy: str
    repair_task: str
    repair_predicate: str
    repair_rules: tuple[str, ...]
    output_schema_fields: tuple[tuple[str, str], ...]
    output_schema_version: str
    schema_sha256: str
    repair_rules_sha256: str
    predicate_sha256: str
    contract_sha256: str

    def __post_init__(self) -> None:
        if self.plan_version != OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION:
            raise StagePlanError("candidate repair plan version is unsupported")
        if (
            isinstance(self.maximum_candidate_count, bool)
            or not isinstance(self.maximum_candidate_count, int)
            or self.maximum_candidate_count != MAX_OUTLINE_CANDIDATE_COUNT
        ):
            raise StagePlanError("candidate repair maximum differs from its producer contract")
        if isinstance(self.candidate_count, bool) or not isinstance(self.candidate_count, int):
            raise StagePlanError("candidate repair plan candidate_count must be an integer")
        if not 1 <= self.candidate_count <= self.maximum_candidate_count:
            raise StagePlanError(
                f"candidate repair plan candidate_count must be within 1..{self.maximum_candidate_count}"
            )
        if not isinstance(self.semantic_repair_enabled, bool):
            raise StagePlanError("candidate repair plan enabled flag must be boolean")
        if self.scope != SEMANTIC_REPAIR_SCOPE_V1:
            raise StagePlanError("candidate repair plan scope is unsupported")
        if self.request_builder_id != SEMANTIC_REPAIR_REQUEST_BUILDER_ID:
            raise StagePlanError("candidate repair request builder identity is unsupported")
        if self.repair_task != SEMANTIC_REPAIR_TASK_V1:
            raise StagePlanError("candidate repair task contract is unsupported")
        if self.repair_predicate != SEMANTIC_REPAIR_PREDICATE_V1:
            raise StagePlanError("candidate repair predicate contract is unsupported")
        if self.output_schema_version != SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1:
            raise StagePlanError("candidate repair output schema version is unsupported")
        if dict(self.output_schema_fields) != SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1:
            raise StagePlanError("candidate repair output schema differs from its versioned contract")
        if self.repair_rules != SEMANTIC_REPAIR_RULES_V1:
            raise StagePlanError("candidate repair rules differ from their versioned contract")
        identity = tuple(str(item).strip() for item in self.route_identity)
        if len(identity) < 3 or any(not item for item in identity[:3]):
            raise StagePlanError("candidate repair plan route identity is incomplete")
        if (self.provider, self.model, self.endpoint_type) != identity[:3]:
            raise StagePlanError("candidate repair plan profile does not match its route")
        if self.effective_input_cap <= 0 or self.effective_input_cap > self.profile_input_budget:
            raise StagePlanError("candidate repair effective input cap exceeds the route profile")
        if self.output_tokens_per_call < 0 or self.reasoning_tokens_per_call < 0:
            raise StagePlanError("candidate repair output and reasoning bounds must be non-negative")
        if self.safety_margin_tokens_per_call < 0:
            raise StagePlanError("candidate repair safety margin must be non-negative")
        if (
            isinstance(self.retry_attempts_per_call_upper_bound, bool)
            or not isinstance(self.retry_attempts_per_call_upper_bound, int)
            or self.retry_attempts_per_call_upper_bound < 0
        ):
            raise StagePlanError("candidate repair retry bound must be a non-negative integer")
        if (
            isinstance(self.wall_seconds_per_call_upper_bound, bool)
            or not math.isfinite(float(self.wall_seconds_per_call_upper_bound))
            or self.wall_seconds_per_call_upper_bound <= 0
        ):
            raise StagePlanError("candidate repair deadline must be finite and positive")
        for label, value in (
            ("safe route config fingerprint", self.route_config_fingerprint_sha256),
            ("config source", self.config_source_sha256),
            ("runtime spec", self.runtime_spec_sha256),
            ("canonical source authority", self.canonical_source_authority_sha256),
            ("schema", self.schema_sha256),
            ("repair rules", self.repair_rules_sha256),
            ("predicate", self.predicate_sha256),
            ("contract", self.contract_sha256),
        ):
            _require_sha256(value, label)
        if not self.config_source_id or not self.canonical_source_authority_id:
            raise StagePlanError("candidate repair plan requires config and canonical source identities")

        schema_payload = dict(self.output_schema_fields)
        if hash_json(schema_payload) != self.schema_sha256:
            raise StagePlanError("candidate repair schema hash does not match its contract")
        if hash_json(list(self.repair_rules)) != self.repair_rules_sha256:
            raise StagePlanError("candidate repair rules hash does not match their contract")
        predicate_hash = _predicate_contract_hash(
            scope=self.scope,
            task=self.repair_task,
            predicate=self.repair_predicate,
            repair_rules=self.repair_rules,
            output_schema_sha256=self.schema_sha256,
        )
        if predicate_hash != self.predicate_sha256:
            raise StagePlanError("candidate repair predicate hash does not match its contract")
        if hash_json(self._contract_payload()) != self.contract_sha256:
            raise StagePlanError("candidate repair contract hash does not match its fields")

    def _assert_current_contract(self) -> None:
        if (
            OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION != self.plan_version
            or MAX_OUTLINE_CANDIDATE_COUNT != self.maximum_candidate_count
            or SEMANTIC_REPAIR_SCOPE_V1 != self.scope
            or SEMANTIC_REPAIR_REQUEST_BUILDER_ID != self.request_builder_id
            or SEMANTIC_REPAIR_TASK_V1 != self.repair_task
            or SEMANTIC_REPAIR_PREDICATE_V1 != self.repair_predicate
            or SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1 != self.output_schema_version
            or tuple(SEMANTIC_REPAIR_RULES_V1) != self.repair_rules
            or tuple(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1.items()) != self.output_schema_fields
            or hash_json(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1) != self.schema_sha256
            or hash_json(list(SEMANTIC_REPAIR_RULES_V1)) != self.repair_rules_sha256
            or _predicate_contract_hash(
                scope=SEMANTIC_REPAIR_SCOPE_V1,
                task=SEMANTIC_REPAIR_TASK_V1,
                predicate=SEMANTIC_REPAIR_PREDICATE_V1,
                repair_rules=SEMANTIC_REPAIR_RULES_V1,
                output_schema_sha256=hash_json(SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1),
            )
            != self.predicate_sha256
        ):
            raise StagePlanError("candidate repair contract changed after plan creation")
        if hash_json(self._contract_payload()) != self.contract_sha256:
            raise StagePlanError("candidate repair contract hash changed after plan creation")

    def _contract_payload(self) -> dict[str, Any]:
        return _candidate_repair_contract_payload(
            plan_version=self.plan_version,
            maximum_candidate_count=self.maximum_candidate_count,
            scope=self.scope,
            candidate_count=self.candidate_count,
            semantic_repair_enabled=self.semantic_repair_enabled,
            request_builder_id=self.request_builder_id,
            task=self.repair_task,
            predicate=self.repair_predicate,
            output_schema_version=self.output_schema_version,
            repair_rules_sha256=self.repair_rules_sha256,
            output_schema_sha256=self.schema_sha256,
            predicate_sha256=self.predicate_sha256,
            route_identity=self.route_identity,
            route_config_fingerprint_sha256=self.route_config_fingerprint_sha256,
            profile=_profile_contract_payload(
                provider=self.provider,
                model=self.model,
                endpoint_type=self.endpoint_type,
                model_context_limit=self.model_context_limit,
                verified_context_limit=self.verified_context_limit,
                input_budget=self.profile_input_budget,
                max_output_tokens=self.output_tokens_per_call,
                reasoning_reserve=self.reasoning_tokens_per_call,
                safety_margin=self.safety_margin_tokens_per_call,
                tokenizer_strategy=self.profile_tokenizer_strategy,
            ),
            effective_input_cap=self.effective_input_cap,
            retry_attempts_per_call_upper_bound=self.retry_attempts_per_call_upper_bound,
            wall_seconds_per_call_upper_bound=self.wall_seconds_per_call_upper_bound,
            config_source_id=self.config_source_id,
            config_source_sha256=self.config_source_sha256,
            runtime_spec_sha256=self.runtime_spec_sha256,
            canonical_source_authority_id=self.canonical_source_authority_id,
            canonical_source_authority_sha256=self.canonical_source_authority_sha256,
        )

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(f"candidate_{index}" for index in range(1, self.candidate_count + 1))

    @property
    def repairable_candidate_ids(self) -> tuple[str, ...]:
        return self.candidate_ids if self.semantic_repair_enabled else ()

    @property
    def maximum_repair_calls(self) -> int:
        return len(self.repairable_candidate_ids)

    @property
    def context_tokens_per_call_upper_bound(self) -> int:
        return (
            self.effective_input_cap
            + self.output_tokens_per_call
            + self.reasoning_tokens_per_call
            + self.safety_margin_tokens_per_call
        )

    def cardinality_basis(self) -> ProviderExposureCardinalityBasisV1:
        self._assert_current_contract()
        return ProviderExposureCardinalityBasisV1(
            basis_artifact=f"{self.plan_version}:{self.contract_sha256}",
            basis_artifact_sha256=self.contract_sha256,
            maximum_count=self.maximum_repair_calls,
            derivation_rule=(
                "one primary semantic repair after candidate contract failure per "
                "member of the materialized primary candidate call graph; a second "
                "failure terminates that candidate"
            ),
            output_schema=self.output_schema_version,
        )

    def to_exposure(self) -> UnplannedProviderExposureV1 | None:
        self._assert_current_contract()
        if not self.semantic_repair_enabled:
            return None
        return UnplannedProviderExposureV1(
            stage_name="outline",
            semantic_role="candidate_provider_generation",
            reason="primary_candidate_semantic_repair_requires_structural_validation_failure",
            request_builder_id=self.request_builder_id,
            route_identity=self.route_identity,
            conditional_on=self.repair_predicate,
            logical_calls_upper_bound=self.maximum_repair_calls,
            input_tokens_per_call_upper_bound=self.effective_input_cap,
            output_tokens_per_call_upper_bound=self.output_tokens_per_call,
            reasoning_tokens_per_call_upper_bound=self.reasoning_tokens_per_call,
            retry_attempts_per_call_upper_bound=self.retry_attempts_per_call_upper_bound,
            wall_seconds_per_call_upper_bound=self.wall_seconds_per_call_upper_bound,
            exposure_status="bounded_conditional",
            cardinality_basis=self.cardinality_basis(),
            context_tokens_per_call_upper_bound=self.context_tokens_per_call_upper_bound,
            context_limit_tokens_per_call=self.verified_context_limit,
        )

    def admit_primary_repair(
        self,
        candidate_id: str,
        *,
        attempted_candidate_ids: MutableSet[str],
        scope: str = SEMANTIC_REPAIR_SCOPE_V1,
    ) -> None:
        """Consume this candidate's sole primary repair opportunity."""

        self._assert_current_contract()
        candidate = str(candidate_id or "").strip()
        if scope != self.scope:
            raise StagePlanError(
                "primary candidate repair plan cannot admit stability-prefixed candidates"
            )
        if not self.semantic_repair_enabled:
            raise StagePlanError("semantic repair is disabled by the bound candidate plan")
        if candidate not in self.repairable_candidate_ids:
            raise StagePlanError("candidate is outside the bound primary repair scope")
        if candidate in attempted_candidate_ids:
            raise StagePlanError("primary candidate semantic repair was already attempted")
        attempted_candidate_ids.add(candidate)

    def materialize_request_row(
        self,
        candidate_id: str,
        request_payload: Mapping[str, Any],
        *,
        route: Any,
        route_config_fingerprint_sha256: str,
        profile: ProviderContextProfile,
        retry_attempts: int,
        wall_seconds_upper_bound: float,
        config_source_id: str,
        config_source_sha256: str,
        runtime_spec_sha256: str,
        canonical_source_authority_id: str,
        canonical_source_authority_sha256: str,
        request_id: str | None = None,
        scope: str = SEMANTIC_REPAIR_SCOPE_V1,
    ) -> ProviderRequestPlanRowV1:
        """Estimate one exact repair request and prove it fits this envelope."""

        self._assert_current_contract()
        candidate = str(candidate_id or "").strip()
        if scope != self.scope:
            raise StagePlanError(
                "primary candidate repair plan cannot materialize stability-prefixed requests"
            )
        if candidate not in self.repairable_candidate_ids:
            raise StagePlanError("materialized repair candidate is outside the bound primary scope")
        if not isinstance(request_payload, Mapping):
            raise StagePlanError("materialized candidate repair request must be an object")
        if (
            request_payload.get("task") != self.repair_task
            or str(request_payload.get("candidate_id") or "") != candidate
            or request_payload.get("output_schema") != dict(self.output_schema_fields)
            or request_payload.get("repair_rules") != list(self.repair_rules)
        ):
            raise StagePlanError("materialized candidate repair request changed its bound schema or identity")
        if tuple(_route_identity(route)) != self.route_identity:
            raise StagePlanError("materialized candidate repair route differs from the initial bound route")
        if _require_sha256(
            route_config_fingerprint_sha256,
            "safe route config fingerprint",
        ) != self.route_config_fingerprint_sha256:
            raise StagePlanError("materialized candidate repair route config changed")
        if (
            str(config_source_id or "").strip() != self.config_source_id
            or _require_sha256(config_source_sha256, "config source")
            != self.config_source_sha256
            or _require_sha256(runtime_spec_sha256, "runtime spec")
            != self.runtime_spec_sha256
            or str(canonical_source_authority_id or "").strip()
            != self.canonical_source_authority_id
            or _require_sha256(
                canonical_source_authority_sha256,
                "canonical source authority",
            )
            != self.canonical_source_authority_sha256
        ):
            raise StagePlanError("materialized candidate repair config, spec, or source authority changed")
        if (
            profile.provider != self.provider
            or profile.model != self.model
            or profile.endpoint_type != self.endpoint_type
            or profile.model_context_limit != self.model_context_limit
            or profile.verified_context_limit != self.verified_context_limit
            or profile.input_budget != self.profile_input_budget
            or profile.max_output_tokens != self.output_tokens_per_call
            or profile.reasoning_reserve != self.reasoning_tokens_per_call
            or profile.safety_margin != self.safety_margin_tokens_per_call
            or profile.tokenizer_strategy != self.profile_tokenizer_strategy
        ):
            raise StagePlanError("materialized candidate repair provider profile changed")
        if isinstance(retry_attempts, bool) or not isinstance(retry_attempts, int):
            raise StagePlanError("materialized candidate repair retries must be an integer")
        if not 0 <= retry_attempts <= self.retry_attempts_per_call_upper_bound:
            raise StagePlanError("materialized candidate repair retries exceed the initial bound")
        if (
            isinstance(wall_seconds_upper_bound, bool)
            or not isinstance(wall_seconds_upper_bound, (int, float))
            or not math.isfinite(float(wall_seconds_upper_bound))
            or float(wall_seconds_upper_bound) <= 0
        ):
            raise StagePlanError("materialized candidate repair deadline must be positive and finite")
        if float(wall_seconds_upper_bound) > self.wall_seconds_per_call_upper_bound:
            raise StagePlanError("materialized candidate repair deadline exceeds the initial bound")

        expected_request_id = f"{candidate}_semantic_repair"
        if request_id is not None and request_id != expected_request_id:
            raise StagePlanError("primary candidate repair request ID differs from its scoped candidate")
        row = build_provider_request_plan_row_v1(
            stage_name="outline",
            request_id=expected_request_id,
            source_builder=self.request_builder_id,
            route=route,
            request_payload=request_payload,
            profile=profile,
            retry_attempts=retry_attempts,
            requested_output_tokens=self.output_tokens_per_call,
            reasoning_reserve_tokens=self.reasoning_tokens_per_call,
            wall_seconds_upper_bound=float(wall_seconds_upper_bound),
            retry_policy="shared_optional",
        )
        if (
            row.request_estimate.estimated_input_tokens > self.effective_input_cap
            or not row.request_estimate.within_context_budget
        ):
            raise StagePlanError("materialized candidate repair request exceeds its initial context bound")
        return row

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.plan_version,
            "maximum_candidate_count": self.maximum_candidate_count,
            "scope": self.scope,
            "candidate_count": self.candidate_count,
            "candidate_ids": list(self.candidate_ids),
            "semantic_repair_enabled": self.semantic_repair_enabled,
            "repairable_candidate_ids": list(self.repairable_candidate_ids),
            "maximum_repair_calls": self.maximum_repair_calls,
            "repair_attempts_per_candidate": 1,
            "conditional_on": self.repair_predicate,
            "request_builder_id": self.request_builder_id,
            "task": self.repair_task,
            "repair_rules": list(self.repair_rules),
            "output_schema_version": self.output_schema_version,
            "output_schema": dict(self.output_schema_fields),
            "output_schema_sha256": self.schema_sha256,
            "repair_rules_sha256": self.repair_rules_sha256,
            "route_identity": list(self.route_identity),
            "route_config_fingerprint_sha256": self.route_config_fingerprint_sha256,
            "resource_bounds": {
                "input_tokens_per_call": self.effective_input_cap,
                "output_tokens_per_call": self.output_tokens_per_call,
                "reasoning_tokens_per_call": self.reasoning_tokens_per_call,
                "safety_margin_tokens_per_call": self.safety_margin_tokens_per_call,
                "context_tokens_per_call": self.context_tokens_per_call_upper_bound,
                "context_limit_tokens_per_call": self.verified_context_limit,
                "retry_attempts_per_call": self.retry_attempts_per_call_upper_bound,
                "wall_seconds_per_call": self.wall_seconds_per_call_upper_bound,
            },
            "config_source_id": self.config_source_id,
            "config_source_sha256": self.config_source_sha256,
            "runtime_spec_sha256": self.runtime_spec_sha256,
            "canonical_source_authority_id": self.canonical_source_authority_id,
            "canonical_source_authority_sha256": self.canonical_source_authority_sha256,
            "predicate_sha256": self.predicate_sha256,
            "contract_sha256": self.contract_sha256,
        }


def build_primary_candidate_repair_plan_v1(
    *,
    candidate_count: int,
    semantic_repair_enabled: bool,
    route_identity: Sequence[str],
    route_config_fingerprint_sha256: str,
    profile: ProviderContextProfile,
    effective_input_cap: int,
    retry_attempts_per_call_upper_bound: int,
    wall_seconds_per_call_upper_bound: float,
    config_source_id: str,
    config_source_sha256: str,
    runtime_spec_sha256: str,
    canonical_source_authority_id: str,
    canonical_source_authority_sha256: str,
) -> OutlineCandidateRepairPlanV1:
    """Build a primary repair bound from the current producer contract.

    Hash bindings are required inputs. This function does not manufacture
    source, config, spec, route, or provider receipt authority.
    """

    if isinstance(candidate_count, bool) or not isinstance(candidate_count, int):
        raise StagePlanError("candidate repair plan candidate_count must be an integer")
    if not 1 <= candidate_count <= MAX_OUTLINE_CANDIDATE_COUNT:
        raise StagePlanError(
            f"candidate repair plan candidate_count must be within 1..{MAX_OUTLINE_CANDIDATE_COUNT}"
        )
    if not isinstance(semantic_repair_enabled, bool):
        raise StagePlanError("candidate repair plan enabled flag must be boolean")
    identity = tuple(str(item).strip() for item in route_identity)
    if len(identity) < 3 or any(not item for item in identity[:3]):
        raise StagePlanError("candidate repair plan route identity is incomplete")
    if (profile.provider, profile.model, profile.endpoint_type) != identity[:3]:
        raise StagePlanError("candidate repair profile does not match its bound route")
    if isinstance(effective_input_cap, bool) or not isinstance(effective_input_cap, int):
        raise StagePlanError("candidate repair effective input cap must be an integer")
    if not 0 < effective_input_cap <= profile.input_budget:
        raise StagePlanError("candidate repair effective input cap exceeds the route profile")
    if (
        isinstance(retry_attempts_per_call_upper_bound, bool)
        or not isinstance(retry_attempts_per_call_upper_bound, int)
        or retry_attempts_per_call_upper_bound < 0
    ):
        raise StagePlanError("candidate repair retry bound must be a non-negative integer")
    if (
        isinstance(wall_seconds_per_call_upper_bound, bool)
        or not isinstance(wall_seconds_per_call_upper_bound, (int, float))
        or not math.isfinite(float(wall_seconds_per_call_upper_bound))
        or float(wall_seconds_per_call_upper_bound) <= 0
    ):
        raise StagePlanError("candidate repair deadline must be finite and positive")
    config_id = str(config_source_id or "").strip()
    source_id = str(canonical_source_authority_id or "").strip()
    if not config_id or not source_id:
        raise StagePlanError("candidate repair plan requires config and canonical source identities")
    config_hash = _require_sha256(config_source_sha256, "config source")
    spec_hash = _require_sha256(runtime_spec_sha256, "runtime spec")
    source_hash = _require_sha256(canonical_source_authority_sha256, "canonical source authority")
    route_fingerprint = _require_sha256(
        route_config_fingerprint_sha256,
        "safe route config fingerprint",
    )
    schema_fields = tuple(
        (str(key), str(value)) for key, value in SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1.items()
    )
    repair_rules = tuple(SEMANTIC_REPAIR_RULES_V1)
    schema_hash = hash_json(dict(schema_fields))
    repair_rules_hash = hash_json(list(repair_rules))
    predicate_hash = _predicate_contract_hash(
        scope=SEMANTIC_REPAIR_SCOPE_V1,
        task=SEMANTIC_REPAIR_TASK_V1,
        predicate=SEMANTIC_REPAIR_PREDICATE_V1,
        repair_rules=repair_rules,
        output_schema_sha256=schema_hash,
    )
    context_bound = (
        effective_input_cap
        + profile.max_output_tokens
        + profile.reasoning_reserve
        + profile.safety_margin
    )
    if context_bound > profile.verified_context_limit:
        raise StagePlanError("candidate repair context bound exceeds the verified route limit")
    profile_payload = _profile_contract_payload(
        provider=profile.provider,
        model=profile.model,
        endpoint_type=profile.endpoint_type,
        model_context_limit=profile.model_context_limit,
        verified_context_limit=profile.verified_context_limit,
        input_budget=profile.input_budget,
        max_output_tokens=profile.max_output_tokens,
        reasoning_reserve=profile.reasoning_reserve,
        safety_margin=profile.safety_margin,
        tokenizer_strategy=profile.tokenizer_strategy,
    )
    contract_payload = _candidate_repair_contract_payload(
        plan_version=OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION,
        maximum_candidate_count=MAX_OUTLINE_CANDIDATE_COUNT,
        scope=SEMANTIC_REPAIR_SCOPE_V1,
        candidate_count=candidate_count,
        semantic_repair_enabled=semantic_repair_enabled,
        request_builder_id=SEMANTIC_REPAIR_REQUEST_BUILDER_ID,
        task=SEMANTIC_REPAIR_TASK_V1,
        predicate=SEMANTIC_REPAIR_PREDICATE_V1,
        output_schema_version=SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1,
        repair_rules_sha256=repair_rules_hash,
        output_schema_sha256=schema_hash,
        predicate_sha256=predicate_hash,
        route_identity=identity,
        route_config_fingerprint_sha256=route_fingerprint,
        profile=profile_payload,
        effective_input_cap=effective_input_cap,
        retry_attempts_per_call_upper_bound=retry_attempts_per_call_upper_bound,
        wall_seconds_per_call_upper_bound=float(wall_seconds_per_call_upper_bound),
        config_source_id=config_id,
        config_source_sha256=config_hash,
        runtime_spec_sha256=spec_hash,
        canonical_source_authority_id=source_id,
        canonical_source_authority_sha256=source_hash,
    )
    contract_hash = hash_json(contract_payload)
    return OutlineCandidateRepairPlanV1(
        plan_version=OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION,
        maximum_candidate_count=MAX_OUTLINE_CANDIDATE_COUNT,
        candidate_count=candidate_count,
        semantic_repair_enabled=semantic_repair_enabled,
        scope=SEMANTIC_REPAIR_SCOPE_V1,
        request_builder_id=SEMANTIC_REPAIR_REQUEST_BUILDER_ID,
        route_identity=identity,
        route_config_fingerprint_sha256=route_fingerprint,
        provider=profile.provider,
        model=profile.model,
        endpoint_type=profile.endpoint_type,
        model_context_limit=profile.model_context_limit,
        verified_context_limit=profile.verified_context_limit,
        profile_input_budget=profile.input_budget,
        effective_input_cap=effective_input_cap,
        output_tokens_per_call=profile.max_output_tokens,
        reasoning_tokens_per_call=profile.reasoning_reserve,
        safety_margin_tokens_per_call=profile.safety_margin,
        retry_attempts_per_call_upper_bound=retry_attempts_per_call_upper_bound,
        wall_seconds_per_call_upper_bound=float(wall_seconds_per_call_upper_bound),
        config_source_id=config_id,
        config_source_sha256=config_hash,
        runtime_spec_sha256=spec_hash,
        canonical_source_authority_id=source_id,
        canonical_source_authority_sha256=source_hash,
        profile_tokenizer_strategy=profile.tokenizer_strategy,
        repair_task=SEMANTIC_REPAIR_TASK_V1,
        repair_predicate=SEMANTIC_REPAIR_PREDICATE_V1,
        repair_rules=repair_rules,
        output_schema_fields=schema_fields,
        output_schema_version=SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1,
        schema_sha256=schema_hash,
        repair_rules_sha256=repair_rules_hash,
        predicate_sha256=predicate_hash,
        contract_sha256=contract_hash,
    )


__all__ = [
    "MAX_OUTLINE_CANDIDATE_COUNT",
    "OUTLINE_CANDIDATE_REPAIR_PLAN_VERSION",
    "SEMANTIC_REPAIR_OUTPUT_SCHEMA_V1",
    "SEMANTIC_REPAIR_OUTPUT_SCHEMA_VERSION_V1",
    "SEMANTIC_REPAIR_PREDICATE_V1",
    "SEMANTIC_REPAIR_REQUEST_BUILDER_ID",
    "SEMANTIC_REPAIR_RULES_V1",
    "SEMANTIC_REPAIR_SCOPE_V1",
    "SEMANTIC_REPAIR_SOURCE_BUILDER",
    "SEMANTIC_REPAIR_TASK_V1",
    "OutlineCandidateRepairPlanV1",
    "build_primary_candidate_repair_plan_v1",
]

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, cast

from ai_interface import _call_ai_api
from models import APIConfig
from runtime.provider_runtime import ProviderBudgetExceeded, ProviderRuntime


SOURCE_GROUNDED_TIERS = frozenset(
    {
        "locator_page_index",
        "preprocess_chunks",
        "normalized_text",
        "plain_text_fallback",
        "visual_refs",
    }
)


@dataclass(frozen=True)
class AdjudicationPacket:
    citation_set_key: str
    stage: str
    claim_text: str
    claim_context: str
    block_context: str
    claim_type: str
    claim_type_confidence: float
    claim_type_rationale: str
    paper_ids: List[str]
    claim_units: List[Dict[str, Any]]
    target_claim_unit: Dict[str, Any]
    claim_unit_results: List[Dict[str, Any]]
    paper_identity_hints: Dict[str, Dict[str, Any]]
    per_paper_evidence_packets: Dict[str, Dict[str, List[Dict[str, Any]]]]
    evidence_excerpt_list: List[str]
    trimmed_candidate_counts: Dict[str, int]
    evidence_status: str
    disposition: str


def _candidate_sort_key(candidate: Dict[str, Any]) -> tuple[int, float, str]:
    source_grounded = bool(candidate.get("source_grounded"))
    confidence = float(candidate.get("confidence") or 0.0)
    resolver_tier = str(candidate.get("resolver_tier") or "")
    return (0 if source_grounded else 1, -confidence, resolver_tier)


def _trim_candidates_for_stage(
    candidates: List[Dict[str, Any]],
    *,
    stage: str,
) -> List[Dict[str, Any]]:
    max_candidates = 6 if stage == "stronger" else 4
    max_summary_hints = 2 if stage == "stronger" else 1
    deduped: List[Dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in sorted(candidates, key=_candidate_sort_key):
        key = (
            candidate.get("resolver_tier"),
            candidate.get("match_reason"),
            candidate.get("text_excerpt"),
            tuple(candidate.get("page_span") or []),
            tuple(candidate.get("chunk_ids") or []),
            candidate.get("caption_excerpt"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)

    source_grounded = [item for item in deduped if item.get("resolver_tier") in SOURCE_GROUNDED_TIERS]
    summary_only = [item for item in deduped if item.get("resolver_tier") == "ai_summary"]
    others = [
        item
        for item in deduped
        if item.get("resolver_tier") not in SOURCE_GROUNDED_TIERS
        and item.get("resolver_tier") != "ai_summary"
    ]

    selected: List[Dict[str, Any]] = []
    if source_grounded:
        selected.extend(source_grounded[:max_candidates])
    else:
        selected.extend(others[:max_candidates])

    remaining_slots = max(max_candidates - len(selected), 0)
    if remaining_slots:
        selected.extend(summary_only[: min(max_summary_hints, remaining_slots)])
        remaining_slots = max(max_candidates - len(selected), 0)

    if remaining_slots:
        spillover = [
            item
            for item in (source_grounded[max_candidates:] + others[max_candidates:] + summary_only[max_summary_hints:])
            if item not in selected
        ]
        selected.extend(spillover[:remaining_slots])

    return selected[:max_candidates]


def build_adjudication_packet(result: Any, *, stage: str = "primary") -> AdjudicationPacket:
    claim_type = str(result.details.get("claim_type") or getattr(result, "claim_type", "") or "result").strip() or "result"
    claim_type_confidence = float(result.details.get("claim_type_confidence") or getattr(result, "claim_type_confidence", 0.0) or 0.0)
    claim_type_rationale = str(result.details.get("claim_type_rationale") or "").strip()
    raw_packets = dict(result.details.get("per_paper_evidence_packets") or {})
    checked_paper_ids = [
        str(item).strip()
        for item in result.details.get("checked_paper_ids", [])
        if str(item).strip()
    ]
    packet_paper_ids = list(dict.fromkeys(checked_paper_ids or getattr(result, "paper_ids", []) or list(raw_packets.keys())))
    trimmed_candidate_counts: Dict[str, int] = {}
    per_paper_packets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    target_claim_unit_id = str((getattr(result, "target_claim_unit", {}) or {}).get("claim_unit_id") or "")
    for paper_id in packet_paper_ids:
        paper_packets = raw_packets.get(paper_id, {})
        if isinstance(paper_packets, list):
            normalized_packets = {target_claim_unit_id or "claim_unit": [dict(item) for item in paper_packets]}
        else:
            normalized_packets = {
                str(claim_unit_id): [dict(item) for item in candidates]
                for claim_unit_id, candidates in dict(paper_packets or {}).items()
            }
        trimmed_by_claim: Dict[str, List[Dict[str, Any]]] = {}
        for claim_unit_id, candidates in normalized_packets.items():
            trimmed = _trim_candidates_for_stage(candidates, stage=stage)
            trimmed_candidate_counts[f"{paper_id}:{claim_unit_id}"] = max(len(candidates) - len(trimmed), 0)
            trimmed_by_claim[claim_unit_id] = trimmed
        per_paper_packets[paper_id] = trimmed_by_claim

    evidence_excerpt_list: List[str] = []
    for claim_packets in per_paper_packets.values():
        for packets in claim_packets.values():
            for candidate in packets:
                excerpt = str(candidate.get("text_excerpt") or candidate.get("caption_excerpt") or "").strip()
                if excerpt and excerpt not in evidence_excerpt_list:
                    evidence_excerpt_list.append(excerpt)
                if len(evidence_excerpt_list) >= (12 if stage == "stronger" else 8):
                    break
            if len(evidence_excerpt_list) >= (12 if stage == "stronger" else 8):
                break
        if len(evidence_excerpt_list) >= (12 if stage == "stronger" else 8):
            break

    return AdjudicationPacket(
        citation_set_key=str(getattr(result, "citation_set_key", "") or result.details.get("citation_set_key") or ""),
        stage=stage,
        claim_text=str(getattr(result, "claim_text", "") or ""),
        claim_context=str(getattr(result, "claim_context", "") or ""),
        block_context=str(getattr(result, "block_context", "") or result.details.get("block_context") or ""),
        claim_type=claim_type,
        claim_type_confidence=claim_type_confidence,
        claim_type_rationale=claim_type_rationale,
        paper_ids=packet_paper_ids,
        claim_units=[dict(item) for item in getattr(result, "claim_units", []) or []],
        target_claim_unit=dict(getattr(result, "target_claim_unit", {}) or {}),
        claim_unit_results=[dict(item) for item in result.details.get("claim_unit_results", []) or []],
        paper_identity_hints={key: dict(value) for key, value in (result.details.get("paper_identity_hints") or {}).items()},
        per_paper_evidence_packets=per_paper_packets,
        evidence_excerpt_list=evidence_excerpt_list,
        trimmed_candidate_counts=trimmed_candidate_counts,
        evidence_status=str(getattr(result, "evidence_status", "") or result.details.get("evidence_status") or ""),
        disposition=str(getattr(result, "disposition", "") or result.details.get("disposition") or ""),
    )


def _build_prompts(packet: AdjudicationPacket) -> tuple[str, str]:
    packet_json = json.dumps(asdict(packet), ensure_ascii=False, indent=2)
    stage_label = "stronger" if packet.stage == "stronger" else "primary"
    prompt = (
        f"You are the {stage_label} adjudication stage for a literature-review citation bundle.\n"
        "Your job is to judge whether the cited claim bundle is supportable by the exact cited paper set.\n"
        "Important rules:\n"
        "- The validator judges cited claims only, not uncited bridge prose.\n"
        "- Multiple cited papers may jointly support different sub-claims inside the same sentence.\n"
        "- Do not require every paper to support the full sentence if the set jointly supports it.\n"
        "- Respect checked_paper_ids and expected_supporting_paper_ids on each claim_unit_result; do not infer claim-paper mappings from pooled citations.\n"
        "- If a claim_unit_result reason is ambiguous_claim_paper_alignment, keep it as evidence_gap or low_confidence/manual_review unless source identity is truly missing or wrong.\n"
        "- Distinguish result, synthesis, future-direction, and limitation/method critique claims.\n"
        "- If the evidence is still not strong enough, use an uncertainty/manual-review outcome instead of forcing unsupported.\n"
        "- Prefer source-grounded evidence packets over summary-only hints when both are available.\n"
        "- ai_summary packets are hints/context only and cannot by themselves justify clean source-grounded support.\n\n"
        f"Bundle packet:\n{packet_json}"
    )
    if packet.stage == "stronger":
        prompt += (
            "\n\nThis is the stronger escalation pass. Re-evaluate carefully before leaving the item in manual review. "
            "Only keep manual review when the packet remains genuinely uncertain after deeper reasoning."
        )

    system_prompt = (
        "Return JSON only with keys: status, confidence, repair_scope, disposition, low_confidence, reasoning, "
        "repair_hint, summary_paper_ids, manual_review_reason, claim_type, claim_type_confidence, claim_type_rationale, adjudication_status. "
        "status must be one of supported, partial_support, evidence_gap, unsupported, contradicted, wrong_source, low_confidence. "
        "repair_scope must be one of none, summary, review, both, manual_review. "
        "disposition must be one of keep_as_is, narrowed_and_kept, manual_review, fail, summary_repair, review_repair, both_repair. "
        "adjudication_status should summarize the semantic outcome (for example supported, evidence_gap, uncertain, wrong_source, unsupported, contradicted)."
    )
    return prompt, system_prompt


def run_adjudication_stage(
    service: Any,
    api_config: Optional[APIConfig],
    packet: AdjudicationPacket,
) -> Optional[Dict[str, Any]]:
    if not api_config or not packet.claim_text.strip() or not packet.paper_ids:
        return None

    try:
        base_max_tokens = int(api_config.get("max_output_tokens", 4096))
        base_temperature = float(api_config.get("temperature", 0.2))
    except Exception:
        base_max_tokens = 4096
        base_temperature = 0.2

    if packet.stage == "stronger":
        max_tokens = max(base_max_tokens, 6144)
        temperature = min(base_temperature, 0.15)
    else:
        max_tokens = base_max_tokens
        temperature = base_temperature

    prompt, system_prompt = _build_prompts(packet)
    packet_payload: Dict[str, Any]
    try:
        packet_payload = asdict(packet)
    except TypeError:
        packet_payload = dict(getattr(packet, "__dict__", {}) or {})

    provider_runtime: Optional[ProviderRuntime] = None
    runtime_factory: Any = getattr(service, "new_provider_runtime", None)
    if callable(runtime_factory):
        packet_hash = hashlib.sha256(
            json.dumps(
                packet_payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:24]
        citation_key = str(packet.citation_set_key or "validation").strip()
        provider_runtime = cast(ProviderRuntime, runtime_factory(
            stage_name="stage4_validate",
            route="Validator_API",
            node_id=f"{packet.stage}:{citation_key}",
            call_id=f"validation:{packet.stage}:{packet_hash}",
            api_config=api_config,
        ))
    call_kwargs: Dict[str, Any] = {
        "max_tokens": max_tokens,
        "temperature": temperature,
        "response_format": "json",
        "logger": getattr(service, "logger", None),
    }
    request_payload: Dict[str, Any] = {
        "system": system_prompt,
        "user": prompt,
        "user_content": None,
        "response_format": "json",
        "max_output_tokens": int(max_tokens),
        "temperature": temperature,
    }
    if provider_runtime is not None:
        call_kwargs["provider_runtime"] = provider_runtime
        bind_call = getattr(service, "bind_provider_call", None)
        if callable(bind_call):
            bind_call(
                call_id=str(provider_runtime.call_id),
                prompt=prompt,
                input_payload=request_payload,
                api_config=api_config,
                schema_hash=str(provider_runtime.schema_hash or ""),
            )
    try:
        report = _call_ai_api(
            prompt,
            api_config,
            system_prompt,
            **call_kwargs,
        )
    except Exception as exc:
        logger = getattr(service, "logger", None)
        if logger:
            logger.warning(f"AI {packet.stage} adjudication failed: {exc}")
        return None

    if provider_runtime is not None and not provider_runtime.receipts:
        # Injected provider callbacks used by the production E2E surface do
        # not pass through ai_interface, so close their runtime explicitly.
        # Real transports already append a receipt inside ai_interface and
        # therefore take the no-op branch above.
        try:
            from runtime.provider_context import ProviderContextProfile

            try:
                context_limit = max(1, int(api_config.get("max_context_tokens") or 128_000))
            except (TypeError, ValueError):
                context_limit = 128_000
            try:
                output_limit = max(1, int(api_config.get("max_output_tokens") or max_tokens))
            except (TypeError, ValueError):
                output_limit = max_tokens
            profile = ProviderContextProfile.conservative(
                provider=str(api_config.get("provider_family") or "configured"),
                model=str(api_config.get("model") or "validator"),
                endpoint_type=str(api_config.get("endpoint_type") or "chat_completions"),
                model_context_limit=context_limit,
                max_output_tokens=output_limit,
            )
            estimate = profile.estimate_request(request_payload)
            admission = provider_runtime.admit(
                estimated_tokens=max(1, int(estimate["estimated_input_tokens"]))
            )
            provider_runtime.complete(
                admission=admission,
                prompt=prompt,
                input_payload=request_payload,
                api_config=api_config,
                result={
                    "status": "success" if isinstance(report, dict) else "failed",
                    "content": report if isinstance(report, dict) else None,
                    "finish_reason": "stop" if isinstance(report, dict) else "",
                    "usage_status": "reported",
                    "error_kind": None if isinstance(report, dict) else "invalid_response",
                },
                metadata={"execution_mode": "injected_adjudicator"},
            )
        except ProviderBudgetExceeded:
            provider_runtime.blocked_receipt(
                prompt=prompt,
                input_payload=request_payload,
                api_config=api_config,
                message="validation adjudicator did not produce a provider receipt before its budget closed",
            )

    if not isinstance(report, dict):
        return None
    bind_output = getattr(service, "bind_provider_output", None)
    if provider_runtime is not None and callable(bind_output):
        bind_output(call_id=str(provider_runtime.call_id), content=report)
    report.setdefault("adjudication_stage", packet.stage)
    report.setdefault("claim_type", packet.claim_type)
    report.setdefault("claim_type_confidence", packet.claim_type_confidence)
    report.setdefault("claim_type_rationale", packet.claim_type_rationale)
    report.setdefault("adjudication_status", str(report.get("status") or packet.evidence_status or "evidence_gap"))
    return report

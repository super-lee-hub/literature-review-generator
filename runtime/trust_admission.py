"""Secret-safe external-host admission for runtime and acceptance execution.

Provider preflight already derives the exact routes reachable from a stage
plan.  This module turns that projection into one deterministic acknowledgement
contract so that direct ``run``/``resume`` cannot skip the confirmation that a
standalone micro-probe performs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from ai_interface import classify_provider_endpoint
from runtime.provider_routes import ReachableProviderRoutePlan
from services.settings import mineru_remote_requested


ACKNOWLEDGEMENT_SCHEMA_VERSION = "external-host-acknowledgement-v1"
POLICY_SCHEMA_VERSION = "external-host-policy-v1"


class ExternalHostAdmissionError(ValueError):
    """Raised before a configured external transport can be constructed."""


def _host_from_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return ""
    host = str(parsed.hostname or "").casefold()
    if not host:
        return ""
    try:
        # Validate malformed ports without treating a port number as a distinct
        # acknowledgement host. Acceptance plans use the same DNS-host-only
        # representation as provider endpoint classification.
        _ = parsed.port
    except ValueError:
        return ""
    return host


def _normalize_host(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Configuration allowlists frequently contain bare hosts.  A URL is also
    # accepted for acknowledgements, but paths, credentials, and query values
    # are intentionally discarded.
    if "://" in raw:
        return _host_from_url(raw)
    return raw.casefold().rstrip(".")


def _is_local_host(host: str) -> bool:
    normalized = _normalize_host(host).split(":", 1)[0]
    return normalized in {"localhost", "127.0.0.1", "::1"}


def _json_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ExternalHostTargetV1:
    host: str
    purpose: str
    classification: str
    route: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ExternalHostPolicyV1:
    targets: tuple[ExternalHostTargetV1, ...]
    route_fingerprint: str

    @property
    def required_hosts(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(target.host for target in self.targets))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "targets": [target.to_dict() for target in self.targets],
            "required_hosts": list(self.required_hosts),
            "route_fingerprint": self.route_fingerprint,
        }


def build_external_host_policy(
    config: Mapping[str, Mapping[str, Any]],
    route_plan: ReachableProviderRoutePlan,
    *,
    provider_sections: Iterable[str] | None = None,
    include_mineru: bool = True,
) -> ExternalHostPolicyV1:
    """Return the non-local hosts which require owner acknowledgement.

    The output deliberately excludes credentials, authorization headers, and
    full URLs.  Localhost is not an external disclosure target.  A non-official
    LLM endpoint and every configured remote MinerU/origin result host are
    considered external.
    """

    selected_sections = (
        {str(section).strip() for section in provider_sections if str(section).strip()}
        if provider_sections is not None
        else None
    )
    targets: list[ExternalHostTargetV1] = []
    route_identity: list[dict[str, str]] = []
    for route in route_plan.routes:
        if (
            not route.enabled
            or not route.resolved
            or (selected_sections is not None and route.section_name not in selected_sections)
        ):
            continue
        section = config.get(route.section_name, {})
        api_base = str(section.get("api_base") or "") if isinstance(section, Mapping) else ""
        classification = classify_provider_endpoint(api_base, route.provider_family)
        host = _normalize_host(classification.get("host"))
        route_identity.append(
            {
                "semantic_role": route.semantic_role,
                "section": route.section_name,
                "provider_family": route.provider_family,
                "model": route.model,
                "endpoint_type": route.endpoint_type,
                "host": host,
                "proxy_mode": str(section.get("proxy_mode") or "environment")
                if isinstance(section, Mapping)
                else "environment",
                "classification": str(classification.get("classification") or ""),
            }
        )
        if host and not _is_local_host(host) and classification.get("classification") != "official_provider_host":
            targets.append(
                ExternalHostTargetV1(
                    host=host,
                    purpose="provider",
                    classification=str(classification.get("classification") or "third_party_gateway"),
                    route=route.semantic_role,
                )
            )

    preprocess = config.get("Preprocess", {})
    preprocess = preprocess if isinstance(preprocess, Mapping) else {}
    parser_mode = str(preprocess.get("parser_mode") or "local").strip().casefold()
    primary_parser = str(preprocess.get("primary_parser") or "local").strip().casefold()
    fallback_parser = str(preprocess.get("fallback_parser") or "").strip().casefold()
    allow_local = str(preprocess.get("allow_local_parse_fallback") or "").strip().casefold()
    mineru_base_host = _host_from_url(preprocess.get("mineru_base_url"))
    configured_result_hosts = tuple(
        host
        for host in (
            _normalize_host(item)
            for item in str(preprocess.get("mineru_allowed_url_hosts") or "").split(",")
        )
        if host
    )
    if include_mineru and mineru_remote_requested(parser_mode, primary_parser):
        if mineru_base_host and not _is_local_host(mineru_base_host):
            targets.append(
                ExternalHostTargetV1(
                    host=mineru_base_host,
                    purpose="mineru_api",
                    classification="remote_parser_origin",
                )
            )
        for host in configured_result_hosts:
            if not _is_local_host(host):
                targets.append(
                    ExternalHostTargetV1(
                        host=host,
                        purpose="mineru_result_or_upload",
                        classification="remote_parser_artifact_host",
                    )
                )

    unique_targets: dict[tuple[str, str, str, str], ExternalHostTargetV1] = {}
    for target in targets:
        unique_targets.setdefault(
            (target.host, target.purpose, target.classification, target.route),
            target,
        )
    ordered_targets = tuple(
        sorted(
            unique_targets.values(),
            key=lambda item: (item.host, item.purpose, item.classification, item.route),
        )
    )
    fingerprint_payload = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "routes": sorted(route_identity, key=lambda item: (item["semantic_role"], item["section"])),
        "preprocess": {
            "parser_mode": parser_mode,
            "primary_parser": primary_parser,
            "fallback_parser": fallback_parser,
            "allow_local_parse_fallback": allow_local,
            "mineru_base_host": mineru_base_host,
            "mineru_allowed_url_hosts": sorted(set(configured_result_hosts)),
        },
        "targets": [target.to_dict() for target in ordered_targets],
    }
    return ExternalHostPolicyV1(
        targets=ordered_targets,
        route_fingerprint=_json_hash(fingerprint_payload),
    )


def validate_external_host_acknowledgement(
    policy: ExternalHostPolicyV1,
    acknowledgement: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate a durable exact-host acknowledgement against a live policy."""

    if not policy.required_hosts:
        return {
            "required": False,
            "acknowledged": False,
            "required_hosts": [],
            "route_fingerprint": policy.route_fingerprint,
        }
    if not isinstance(acknowledgement, Mapping):
        raise ExternalHostAdmissionError(
            "external host acknowledgement is required for the configured external routes"
        )
    if acknowledgement.get("schema_version") != ACKNOWLEDGEMENT_SCHEMA_VERSION:
        raise ExternalHostAdmissionError("external host acknowledgement schema is invalid")
    if acknowledgement.get("acknowledged") is not True:
        raise ExternalHostAdmissionError("external host acknowledgement must be explicitly true")
    raw_hosts = acknowledgement.get("hosts")
    if not isinstance(raw_hosts, (list, tuple)) or any(not isinstance(item, str) for item in raw_hosts):
        raise ExternalHostAdmissionError("external host acknowledgement hosts must be a string array")
    normalized_hosts = tuple(sorted({_normalize_host(item) for item in raw_hosts if _normalize_host(item)}))
    required_hosts = tuple(sorted(set(policy.required_hosts)))
    if normalized_hosts != required_hosts:
        raise ExternalHostAdmissionError(
            "external host acknowledgement must exactly match the configured external host set"
        )
    supplied_fingerprint = str(acknowledgement.get("route_fingerprint") or "").strip().lower()
    if supplied_fingerprint != policy.route_fingerprint:
        raise ExternalHostAdmissionError(
            "external host acknowledgement route fingerprint is missing or stale"
        )
    return {
        "required": True,
        "acknowledged": True,
        "required_hosts": list(required_hosts),
        "route_fingerprint": policy.route_fingerprint,
        "schema_version": ACKNOWLEDGEMENT_SCHEMA_VERSION,
    }


def acknowledgement_from_values(
    policy: ExternalHostPolicyV1,
    *,
    acknowledged: bool,
    hosts: Sequence[str],
    route_fingerprint: str = "",
) -> dict[str, Any]:
    """Build the durable form used by compatibility acceptance-plan fields."""

    return {
        "schema_version": ACKNOWLEDGEMENT_SCHEMA_VERSION,
        "acknowledged": bool(acknowledged),
        "hosts": list(hosts),
        "route_fingerprint": str(route_fingerprint or policy.route_fingerprint),
    }


__all__ = [
    "ACKNOWLEDGEMENT_SCHEMA_VERSION",
    "POLICY_SCHEMA_VERSION",
    "ExternalHostAdmissionError",
    "ExternalHostPolicyV1",
    "ExternalHostTargetV1",
    "acknowledgement_from_values",
    "build_external_host_policy",
    "validate_external_host_acknowledgement",
]

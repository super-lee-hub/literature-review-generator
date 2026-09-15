"""Secret-safe external-host admission for runtime and acceptance execution.

Provider preflight already derives the exact routes reachable from a stage
plan.  This module turns that projection into one deterministic acknowledgement
contract so that direct ``run``/``resume`` cannot skip the confirmation that a
standalone micro-probe performs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import posixpath
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from ai_interface import classify_provider_endpoint, resolve_anthropic_messages_url
from runtime.provider_routes import ReachableProviderRoutePlan
from services.mineru_policy import (
    effective_mineru_allowed_url_hosts,
    parse_mineru_http_url,
)
from services.proxy_policy import normalize_proxy_mode
from services.settings import mineru_remote_requested


ACKNOWLEDGEMENT_SCHEMA_VERSION = "external-host-acknowledgement-v2"
ACKNOWLEDGEMENT_VERSION = 2
ACK_MAX_TTL = timedelta(days=7)
ACK_CLOCK_SKEW = timedelta(minutes=2)
POLICY_SCHEMA_VERSION = "external-host-policy-v2"


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
    raw = str(host or "").strip()
    if not raw:
        return False
    candidate = raw
    if "://" not in candidate:
        if candidate.startswith("[") or (
            candidate.count(":") == 1 and candidate.rsplit(":", 1)[1].isdigit()
        ):
            candidate = f"http://{candidate}"
        elif candidate.count(":") > 1:
            try:
                return ipaddress.ip_address(candidate).is_loopback
            except ValueError:
                return False
        else:
            candidate = f"http://{candidate}"
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
        normalized = str(parsed.hostname or "").casefold().rstrip(".")
    except (TypeError, ValueError):
        return False
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _normalized_path(value: str) -> str:
    path = posixpath.normpath("/" + str(value or "").lstrip("/"))
    return "" if path == "/" else path.rstrip("/")


def _endpoint_identity(
    api_base: Any,
    *,
    endpoint_type: str,
    provider_family: str,
    section: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe the exact endpoint URL construction without credentials."""

    raw_base = str(api_base or "").strip()
    try:
        base = parse_mineru_http_url(raw_base)
    except ValueError as exc:
        raise ExternalHostAdmissionError("configured provider endpoint is malformed") from exc
    if base.query or base.fragment or base.username or base.password:
        raise ExternalHostAdmissionError(
            "configured provider endpoint must not contain credentials, query, or fragment"
        )
    endpoint = str(endpoint_type or "").strip().casefold()
    options = section if isinstance(section, Mapping) else {}
    if endpoint == "anthropic":
        request_url = resolve_anthropic_messages_url(
            raw_base, str(options.get("anthropic_path") or "")
        )
    elif endpoint in {"mineru", "mineru_batch"}:
        request_url = raw_base
    else:
        suffix = "responses" if endpoint == "responses" else "chat/completions"
        request_url = f"{raw_base.rstrip('/')}/{suffix}"
    try:
        request = parse_mineru_http_url(request_url)
    except ValueError as exc:
        raise ExternalHostAdmissionError("configured provider request endpoint is malformed") from exc
    if request.query or request.fragment or request.username or request.password:
        raise ExternalHostAdmissionError(
            "configured provider request endpoint must not contain credentials, query, or fragment"
        )
    scheme = request.scheme.casefold()
    port = request.port or (443 if scheme == "https" else 80)
    return {
        "scheme": scheme,
        "host": str(request.hostname or "").casefold().rstrip("."),
        "port": int(port),
        "path": _normalized_path(request.path),
        "provider_family": str(provider_family or "").strip().casefold(),
        "endpoint_type": endpoint,
    }


def _parse_ack_timestamp(value: Any, *, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ExternalHostAdmissionError(f"external host acknowledgement {field_name} is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalHostAdmissionError(
            f"external host acknowledgement {field_name} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise ExternalHostAdmissionError(
            f"external host acknowledgement {field_name} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def _ack_host(value: Any) -> str:
    raw = str(value or "").strip()
    if "://" in raw:
        try:
            parsed = parse_mineru_http_url(raw, require_https=True)
        except ValueError as exc:
            raise ExternalHostAdmissionError(
                "external host acknowledgement contains an invalid host"
            ) from exc
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ExternalHostAdmissionError(
                "external host acknowledgement must contain host names only"
            )
        raw = str(parsed.hostname or "")
    normalized = _normalize_host(raw)
    if not normalized or ":" in normalized or "/" in normalized or "@" in normalized:
        raise ExternalHostAdmissionError("external host acknowledgement contains an invalid host")
    return normalized


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
    route_identity: list[dict[str, Any]] = []
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
        endpoint_identity = _endpoint_identity(
            api_base,
            endpoint_type=route.endpoint_type,
            provider_family=route.provider_family,
            section=section if isinstance(section, Mapping) else None,
        )
        route_identity.append(
            {
                "semantic_role": route.semantic_role,
                "section": route.section_name,
                "provider_family": route.provider_family,
                "model": route.model,
                "endpoint_type": route.endpoint_type,
                "host": host,
                "proxy_mode": normalize_proxy_mode(section.get("proxy_mode"))
                if isinstance(section, Mapping)
                else "environment",
                "classification": str(classification.get("classification") or ""),
                "endpoint_identity": endpoint_identity,
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
    mineru_base_url = str(preprocess.get("mineru_base_url") or "").strip()
    mineru_base_host = _host_from_url(mineru_base_url)
    effective_result_hosts, invalid_result_hosts = effective_mineru_allowed_url_hosts(
        preprocess.get("mineru_allowed_url_hosts")
    )
    mineru_api_endpoint: dict[str, Any] = {}
    mineru_upload_identity: dict[str, Any] = {}
    if invalid_result_hosts:
        raise ExternalHostAdmissionError("MinerU allowed URL host configuration is invalid")
    if include_mineru and mineru_remote_requested(parser_mode, primary_parser):
        if not mineru_base_url:
            raise ExternalHostAdmissionError("MinerU base URL is required for remote parsing")
        mineru_api_endpoint = _endpoint_identity(
            mineru_base_url,
            endpoint_type="mineru_batch",
            provider_family="mineru",
            section={"mineru_upload_endpoint": preprocess.get("mineru_upload_endpoint")},
        )
        try:
            base_url_parts = parse_mineru_http_url(mineru_base_url, require_https=True)
            upload_endpoint = str(
                preprocess.get("mineru_upload_endpoint") or "/file-urls/batch"
            ).strip()
            upload_parts = urlsplit(upload_endpoint)
            if (
                upload_parts.scheme
                or upload_parts.netloc
                or upload_parts.username
                or upload_parts.password
                or upload_parts.query
                or upload_parts.fragment
            ):
                raise ValueError("MinerU upload endpoint must be a relative path")
            upload_url = urlunsplit(
                (
                    base_url_parts.scheme,
                    base_url_parts.netloc,
                    _normalized_path(
                        f"{base_url_parts.path.rstrip('/')}/{upload_parts.path.lstrip('/')}"
                    ),
                    "",
                    "",
                )
            )
            mineru_upload_identity = _endpoint_identity(
                upload_url,
                endpoint_type="mineru_batch",
                provider_family="mineru",
            )
        except (TypeError, ValueError, ExternalHostAdmissionError) as exc:
            raise ExternalHostAdmissionError("MinerU upload endpoint is malformed") from exc
        if mineru_base_host and not _is_local_host(mineru_base_host):
            targets.append(
                ExternalHostTargetV1(
                    host=mineru_base_host,
                    purpose="mineru_api",
                    classification="remote_parser_origin",
                )
            )
        for host in sorted(effective_result_hosts):
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
            "mineru_api_endpoint": mineru_api_endpoint if include_mineru and mineru_remote_requested(parser_mode, primary_parser) else {},
            "mineru_upload_endpoint": mineru_upload_identity if include_mineru and mineru_remote_requested(parser_mode, primary_parser) else {},
            "mineru_allowed_url_hosts": sorted(effective_result_hosts),
            "mineru_model_version": str(preprocess.get("mineru_model_version") or "vlm"),
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
    allowed_fields = {
        "schema_version",
        "version",
        "acknowledged",
        "hosts",
        "route_fingerprint",
        "issued_at",
        "expires_at",
    }
    unknown_fields = sorted(str(key) for key in acknowledgement if str(key) not in allowed_fields)
    if unknown_fields:
        raise ExternalHostAdmissionError(
            "external host acknowledgement contains unknown fields: " + ", ".join(unknown_fields)
        )
    if acknowledgement.get("schema_version") != ACKNOWLEDGEMENT_SCHEMA_VERSION:
        raise ExternalHostAdmissionError("external host acknowledgement schema is invalid")
    if acknowledgement.get("version") != ACKNOWLEDGEMENT_VERSION:
        raise ExternalHostAdmissionError("external host acknowledgement version is invalid")
    if acknowledgement.get("acknowledged") is not True:
        raise ExternalHostAdmissionError("external host acknowledgement must be explicitly true")
    raw_hosts = acknowledgement.get("hosts")
    if not isinstance(raw_hosts, list) or any(not isinstance(item, str) for item in raw_hosts):
        raise ExternalHostAdmissionError("external host acknowledgement hosts must be a string array")
    normalized_values = [_ack_host(item) for item in raw_hosts]
    if len(set(normalized_values)) != len(normalized_values):
        raise ExternalHostAdmissionError(
            "external host acknowledgement hosts must not contain duplicates"
        )
    normalized_hosts = tuple(sorted(normalized_values))
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
    issued_at = _parse_ack_timestamp(acknowledgement.get("issued_at"), field_name="issued_at")
    expires_at = _parse_ack_timestamp(acknowledgement.get("expires_at"), field_name="expires_at")
    now = datetime.now(timezone.utc)
    if issued_at > now + ACK_CLOCK_SKEW:
        raise ExternalHostAdmissionError("external host acknowledgement is issued in the future")
    if expires_at <= issued_at or expires_at <= now:
        raise ExternalHostAdmissionError("external host acknowledgement is expired")
    if expires_at - issued_at > ACK_MAX_TTL:
        raise ExternalHostAdmissionError("external host acknowledgement lifetime is too long")
    return {
        "required": True,
        "acknowledged": True,
        "required_hosts": list(required_hosts),
        "route_fingerprint": policy.route_fingerprint,
        "schema_version": ACKNOWLEDGEMENT_SCHEMA_VERSION,
        "version": ACKNOWLEDGEMENT_VERSION,
        "issued_at": issued_at.isoformat().replace("+00:00", "Z"),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }


def acknowledgement_from_values(
    policy: ExternalHostPolicyV1,
    *,
    acknowledged: bool,
    hosts: Sequence[str],
    route_fingerprint: str = "",
    issued_at: str = "",
    expires_at: str = "",
) -> dict[str, Any]:
    """Build the durable form used by compatibility acceptance-plan fields."""

    if not isinstance(acknowledged, bool):
        raise TypeError("acknowledged must be a boolean")
    now = datetime.now(timezone.utc)
    issued = issued_at or now.isoformat().replace("+00:00", "Z")
    expires = expires_at or (now + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    normalized_hosts = [_ack_host(item) for item in hosts]
    return {
        "schema_version": ACKNOWLEDGEMENT_SCHEMA_VERSION,
        "version": ACKNOWLEDGEMENT_VERSION,
        "acknowledged": acknowledged,
        "hosts": normalized_hosts,
        "route_fingerprint": str(route_fingerprint or policy.route_fingerprint),
        "issued_at": issued,
        "expires_at": expires,
    }


__all__ = [
    "ACKNOWLEDGEMENT_SCHEMA_VERSION",
    "ACKNOWLEDGEMENT_VERSION",
    "POLICY_SCHEMA_VERSION",
    "ExternalHostAdmissionError",
    "ExternalHostPolicyV1",
    "ExternalHostTargetV1",
    "acknowledgement_from_values",
    "build_external_host_policy",
    "validate_external_host_acknowledgement",
]

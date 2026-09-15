"""Secret-safe credential source resolution for runtime configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values  # type: ignore


API_ENV_MAPPING: dict[str, str] = {
    "Primary_Reader_API": "LLM_PRIMARY_READER_API",
    "Backup_Reader_API": "LLM_BACKUP_READER_API",
    "Writer_API": "LLM_WRITER_API",
    "Outline_API": "LLM_OUTLINE_API",
    "Free_Mode_API": "LLM_FREE_MODE_API",
    "Validator_API": "LLM_VALIDATOR_API",
}

# Non-provider runtime settings that can alter where user documents are sent
# or whether a remote parser is used.  These values use the same explicit
# precedence and conflict policy as provider credentials.
PREPROCESS_ENV_MAPPING: dict[str, str] = {
    "MINERU_BASE_URL": "mineru_base_url",
    "MINERU_API_TOKEN": "mineru_api_token",
    "MINERU_MODEL_VERSION": "mineru_model_version",
    "MINERU_UPLOAD_ENDPOINT": "mineru_upload_endpoint",
    "MINERU_POLL_ENDPOINT_TEMPLATES": "mineru_poll_endpoint_templates",
    "MINERU_POLL_INTERVAL_SECONDS": "mineru_poll_interval_seconds",
    "MINERU_POLL_TIMEOUT_SECONDS": "mineru_poll_timeout_seconds",
    "MINERU_REQUEST_TIMEOUT_SECONDS": "mineru_request_timeout_seconds",
    "MINERU_UPLOAD_TIMEOUT_SECONDS": "mineru_upload_timeout_seconds",
    "MINERU_DOWNLOAD_TIMEOUT_SECONDS": "mineru_download_timeout_seconds",
    "MINERU_REQUEST_MAX_RETRIES": "mineru_request_max_retries",
    "MINERU_RETRY_BACKOFF_SECONDS": "mineru_retry_backoff_seconds",
    "MINERU_MAX_REMOTE_TASKS": "mineru_max_remote_tasks",
    "MINERU_MAX_REMOTE_HTTP_CALLS": "mineru_max_remote_http_calls",
    "MINERU_MAX_REMOTE_UPLOAD_BYTES": "mineru_max_remote_upload_bytes",
    "MINERU_RESPONSE_MAX_BYTES": "mineru_response_max_bytes",
    "MINERU_ZIP_MAX_ENTRIES": "mineru_zip_max_entries",
    "MINERU_ZIP_MAX_UNCOMPRESSED_BYTES": "mineru_zip_max_uncompressed_bytes",
    "MINERU_ZIP_MAX_ENTRY_BYTES": "mineru_zip_max_entry_bytes",
    "MINERU_ZIP_MAX_COMPRESSION_RATIO": "mineru_zip_max_compression_ratio",
    "MINERU_JSON_MAX_BYTES": "mineru_json_max_bytes",
    "MINERU_TEXT_MAX_BYTES": "mineru_text_max_bytes",
    "MINERU_SOURCE_PDF_MAX_BYTES": "source_pdf_max_bytes",
    "MINERU_ALLOWED_URL_HOSTS": "mineru_allowed_url_hosts",
    "ALLOW_LOCAL_PARSE_FALLBACK": "allow_local_parse_fallback",
    "DOCLING_TIMEOUT_SECONDS": "docling_timeout_seconds",
    "OCR_TIMEOUT_SECONDS": "ocr_timeout_seconds",
}

_TEMPLATE_CREDENTIAL_RE = re.compile(
    r"^(?:loaded_from_\.env_file|your_.+_api_key_here)$",
    re.IGNORECASE,
)
_DIAGNOSTIC_SECRET_MARKERS = frozenset(
    {"api_key", "apikey", "token", "authorization", "secret", "password", "credential", "userinfo"}
)


def redact_for_diagnostics(value: Any, *, key: str = "") -> Any:
    """Return a shareable diagnostic projection without secret values."""

    folded_key = str(key or "").casefold().replace("-", "_")
    if any(marker in folded_key for marker in _DIAGNOSTIC_SECRET_MARKERS):
        return {"configured": bool(str(value or "").strip())}
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_for_diagnostics(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_for_diagnostics(item, key=key) for item in value]
    if "url" in folded_key or folded_key.endswith("_endpoint") or folded_key.endswith("_base"):
        raw = str(value or "").strip()
        if "://" in raw:
            try:
                parsed = urlsplit(raw)
                if parsed.username or parsed.password:
                    return "[REDACTED_URL]"
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except (TypeError, ValueError):
                return "[REDACTED_URL]"
    return value


class CredentialConflictError(ValueError):
    """Raised when two meaningful credential sources disagree."""


def is_template_credential(value: Any) -> bool:
    return bool(_TEMPLATE_CREDENTIAL_RE.fullmatch(str(value or "").strip()))


def _meaningful(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if not normalized or is_template_credential(normalized) else normalized


@dataclass(frozen=True)
class CredentialProvenance:
    section: str
    env_var: str
    process_env_present: bool
    dotenv_present: bool
    config_present: bool
    selected_source: str
    process_env_equals_dotenv: bool | None
    process_env_equals_config: bool | None
    dotenv_equals_config: bool | None
    conflict: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _comparison(left: str, right: str) -> bool | None:
    if not left or not right:
        return None
    return left == right


def resolve_credentials(
    config: Mapping[str, Mapping[str, Any]],
    *,
    config_path: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[CredentialProvenance, ...]]:
    """Apply the explicit precedence ``process env > .env > config.ini``.

    A template sentinel is not a meaningful secret and therefore does not
    create a false conflict with a real source.  Any two meaningful values
    that differ fail closed before the configuration reaches transport code.
    The returned provenance contains only booleans/source names, never values
    or hashes.
    """

    env = environ if environ is not None else os.environ
    config_dir = Path(config_path).expanduser().resolve().parent
    selected_dotenv = (
        Path(dotenv_path).expanduser().resolve()
        if dotenv_path
        else config_dir / ".env"
    )
    dotenv_payload: Mapping[str, Any] = {}
    if selected_dotenv.is_file():
        dotenv_payload = dotenv_values(selected_dotenv)

    resolved = {
        str(section): dict(values) if isinstance(values, Mapping) else {}
        for section, values in config.items()
    }
    provenance: list[CredentialProvenance] = []
    for section, env_var in API_ENV_MAPPING.items():
        raw_config_value = str(resolved.get(section, {}).get("api_key") or "").strip()
        raw_dotenv_value = str(dotenv_payload.get(env_var) or "").strip()
        raw_process_value = str(env.get(env_var) or "").strip()
        config_value = _meaningful(raw_config_value)
        dotenv_value = _meaningful(raw_dotenv_value)
        process_value = _meaningful(raw_process_value)
        meaningful_values = {
            source: value
            for source, value in (
                ("process_env", process_value),
                ("dotenv", dotenv_value),
                ("config.ini", config_value),
            )
            if value
        }
        distinct_values = set(meaningful_values.values())
        conflict = len(distinct_values) > 1
        if conflict:
            sources = ", ".join(sorted(meaningful_values))
            raise CredentialConflictError(
                f"credential sources conflict for [{section}] ({env_var}); "
                f"meaningful sources={sources}"
            )
        if process_value:
            selected_source, selected_value = "process_env", process_value
        elif dotenv_value:
            selected_source, selected_value = "dotenv", dotenv_value
        elif config_value:
            selected_source, selected_value = "config.ini", config_value
        else:
            selected_source, selected_value = "none", ""

        if section in resolved:
            # Keep template values out of the runtime after a real source was
            # selected, while preserving an empty/template value for template
            # validation and safe diagnostics when no real source exists.
            resolved[section]["api_key"] = selected_value or str(
                resolved[section].get("api_key") or ""
            ).strip()
        provenance.append(
            CredentialProvenance(
                section=section,
                env_var=env_var,
                process_env_present=bool(raw_process_value),
                dotenv_present=bool(raw_dotenv_value),
                config_present=bool(raw_config_value),
                selected_source=selected_source,
                process_env_equals_dotenv=_comparison(process_value, dotenv_value),
                process_env_equals_config=_comparison(process_value, config_value),
                dotenv_equals_config=_comparison(dotenv_value, config_value),
                conflict=False,
            )
        )
    return resolved, tuple(provenance)


def resolve_preprocess_environment(
    config: Mapping[str, Mapping[str, Any]],
    *,
    config_path: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[CredentialProvenance, ...]]:
    """Resolve MinerU/preprocess settings without leaking their values.

    The process environment, adjacent dotenv, and config file must agree when
    more than one meaningful source is present.  This prevents a stale
    process-only token or endpoint from silently routing document uploads to a
    different account or service.
    """

    env = environ if environ is not None else os.environ
    config_dir = Path(config_path).expanduser().resolve().parent
    selected_dotenv = (
        Path(dotenv_path).expanduser().resolve()
        if dotenv_path
        else config_dir / ".env"
    )
    dotenv_payload: Mapping[str, Any] = {}
    if selected_dotenv.is_file():
        dotenv_payload = dotenv_values(selected_dotenv)

    resolved = {
        str(section): dict(values) if isinstance(values, Mapping) else {}
        for section, values in config.items()
    }
    preprocess = resolved.setdefault("Preprocess", {})
    provenance: list[CredentialProvenance] = []
    for env_var, config_key in PREPROCESS_ENV_MAPPING.items():
        raw_config_value = str(preprocess.get(config_key) or "").strip()
        raw_dotenv_value = str(dotenv_payload.get(env_var) or "").strip()
        raw_process_value = str(env.get(env_var) or "").strip()
        config_value = _meaningful(raw_config_value)
        dotenv_value = _meaningful(raw_dotenv_value)
        process_value = _meaningful(raw_process_value)
        meaningful_values = {
            source: value
            for source, value in (
                ("process_env", process_value),
                ("dotenv", dotenv_value),
                ("config.ini", config_value),
            )
            if value
        }
        if len(set(meaningful_values.values())) > 1:
            sources = ", ".join(sorted(meaningful_values))
            raise CredentialConflictError(
                f"preprocess setting sources conflict for [Preprocess] ({env_var}); "
                f"meaningful sources={sources}"
            )
        if process_value:
            selected_source, selected_value = "process_env", process_value
        elif dotenv_value:
            selected_source, selected_value = "dotenv", dotenv_value
        elif config_value:
            selected_source, selected_value = "config.ini", config_value
        else:
            selected_source, selected_value = "none", ""
        if selected_value:
            preprocess[config_key] = selected_value
        provenance.append(
            CredentialProvenance(
                section="Preprocess",
                env_var=env_var,
                process_env_present=bool(raw_process_value),
                dotenv_present=bool(raw_dotenv_value),
                config_present=bool(raw_config_value),
                selected_source=selected_source,
                process_env_equals_dotenv=_comparison(process_value, dotenv_value),
                process_env_equals_config=_comparison(process_value, config_value),
                dotenv_equals_config=_comparison(dotenv_value, config_value),
                conflict=False,
            )
        )
    return resolved, tuple(provenance)


def provenance_payload(items: tuple[CredentialProvenance, ...] | list[CredentialProvenance]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]


__all__ = [
    "API_ENV_MAPPING",
    "PREPROCESS_ENV_MAPPING",
    "CredentialConflictError",
    "CredentialProvenance",
    "is_template_credential",
    "provenance_payload",
    "redact_for_diagnostics",
    "resolve_credentials",
    "resolve_preprocess_environment",
]

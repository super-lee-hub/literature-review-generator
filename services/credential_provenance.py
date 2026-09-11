"""Secret-safe credential source resolution for runtime configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
from typing import Any, Mapping

from dotenv import dotenv_values  # type: ignore


API_ENV_MAPPING: dict[str, str] = {
    "Primary_Reader_API": "LLM_PRIMARY_READER_API",
    "Backup_Reader_API": "LLM_BACKUP_READER_API",
    "Writer_API": "LLM_WRITER_API",
    "Outline_API": "LLM_OUTLINE_API",
    "Free_Mode_API": "LLM_FREE_MODE_API",
    "Validator_API": "LLM_VALIDATOR_API",
}

_TEMPLATE_CREDENTIAL_RE = re.compile(
    r"^(?:loaded_from_\.env_file|your_.+_api_key_here)$",
    re.IGNORECASE,
)


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


def provenance_payload(items: tuple[CredentialProvenance, ...] | list[CredentialProvenance]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]

"""Configuration helpers for GUI setup and modernized defaults."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from typing import Dict, Mapping, MutableMapping

from config_validator import test_api_connection
from services.settings import ApplicationSettings, CONFIG_SCHEMA_VERSION, validate_config_keys


OUTLINE_QUALITY_GATE_DEFAULTS: Dict[str, str] = {
    "coverage_scope": "full",
    "min_canonical_coverage_full": "0.50",
    "min_canonical_coverage_local": "0.25",
    "min_effective_sections": "3",
    "max_duplicate_assignments": "0",
    "block_placeholder_sections": "true",
    "block_empty_research_streams": "true",
}


@dataclass(frozen=True)
class ProviderPreset:
    """Preset metadata used by the GUI setup flow."""

    label: str
    default_api_base: str
    append_v1: bool = True


PROVIDER_PRESETS: Dict[str, ProviderPreset] = {
    "custom": ProviderPreset("Custom", "", append_v1=False),
    "openai": ProviderPreset("OpenAI", "https://api.openai.com/v1"),
    "openai_compatible": ProviderPreset("OpenAI Compatible", "https://api.openai.com/v1"),
    "aihubmix": ProviderPreset("AIHubMix", "https://aihubmix.com/v1"),
    "deepseek": ProviderPreset("DeepSeek", "https://api.deepseek.com", append_v1=False),
    "siliconflow": ProviderPreset("SiliconFlow", "https://api.siliconflow.cn/v1"),
    "videocaptioner": ProviderPreset("VideoCaptioner", "https://api.videocaptioner.cn/v1"),
    "ollama": ProviderPreset("Ollama", "http://localhost:11434", append_v1=False),
}

API_ENV_MAPPING: Dict[str, str] = {
    "Primary_Reader_API": "LLM_PRIMARY_READER_API",
    "Backup_Reader_API": "LLM_BACKUP_READER_API",
    "Writer_API": "LLM_WRITER_API",
    "Outline_API": "LLM_OUTLINE_API",
    "Free_Mode_API": "LLM_FREE_MODE_API",
    "Validator_API": "LLM_VALIDATOR_API",
}

MINERU_ENV_KEYS = [
    "MINERU_BASE_URL",
    "MINERU_API_TOKEN",
    "MINERU_MODEL_VERSION",
    "MINERU_UPLOAD_ENDPOINT",
    "MINERU_POLL_ENDPOINT_TEMPLATES",
    "MINERU_POLL_INTERVAL_SECONDS",
    "MINERU_POLL_TIMEOUT_SECONDS",
    "MINERU_REQUEST_MAX_RETRIES",
    "MINERU_RETRY_BACKOFF_SECONDS",
    "ALLOW_LOCAL_PARSE_FALLBACK",
]


def default_config_sections() -> Dict[str, Dict[str, str]]:
    """Return a config layout that includes all new sections."""

    return {
        "Application": {
            "config_schema": str(CONFIG_SCHEMA_VERSION),
        },
        "Paths": {
            "zotero_report": "",
            "library_path": "",
            "output_path": "./output",
        },
        "Primary_Reader_API": {
            "api_key": "loaded_from_.env_file",
            "model": "deepseek-v4-pro",
            "api_base": PROVIDER_PRESETS["deepseek"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "thinking": "enabled",
            "reasoning_effort": "max",
            "max_context_tokens": "1000000",
            "max_output_tokens": "3000",
            "temperature": "0.3",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "600",
            "total_timeout_seconds": "600",
            "first_token_timeout_seconds": "120",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "2048",
            "safety_margin_tokens": "1024",
            "force_highest_reasoning": "true",
            "omit_temperature_when_reasoning": "true",
        },
        "Backup_Reader_API": {
            "api_key": "loaded_from_.env_file",
            "model": "",
            "api_base": PROVIDER_PRESETS["videocaptioner"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "generic",
            "max_context_tokens": "128000",
            "max_output_tokens": "8192",
            "temperature": "0.3",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "600",
            "total_timeout_seconds": "600",
            "first_token_timeout_seconds": "120",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "0",
            "safety_margin_tokens": "1024",
        },
        "Writer_API": {
            "api_key": "loaded_from_.env_file",
            "model": "gpt-5.5",
            "api_base": PROVIDER_PRESETS["aihubmix"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "responses",
            "provider_family": "aihubmix_openai",
            "reasoning_effort": "high",
            "force_highest_reasoning": "true",
            "text_verbosity": "high",
            "max_context_tokens": "128000",
            "max_output_tokens": "32000",
            "temperature": "0.0",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "900",
            "total_timeout_seconds": "900",
            "first_token_timeout_seconds": "180",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "4096",
            "safety_margin_tokens": "2048",
            "omit_temperature_when_reasoning": "true",
        },
        "Outline_API": {
            "api_key": "loaded_from_.env_file",
            "model": "claude-opus-4-7",
            "api_base": PROVIDER_PRESETS["aihubmix"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "aihubmix_claude",
            "reasoning_effort": "xhigh",
            "reasoning_display": "summarized",
            "max_context_tokens": "200000",
            "max_output_tokens": "16000",
            "temperature": "0.0",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "900",
            "total_timeout_seconds": "900",
            "first_token_timeout_seconds": "180",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "4096",
            "safety_margin_tokens": "2048",
            "force_highest_reasoning": "true",
        },
        "Free_Mode_API": {
            "api_key": "loaded_from_.env_file",
            "model": "",
            "api_base": "",
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "generic",
            "max_context_tokens": "128000",
            "max_output_tokens": "6000",
            "temperature": "0.4",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "600",
            "total_timeout_seconds": "600",
            "first_token_timeout_seconds": "120",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "0",
            "safety_margin_tokens": "1024",
        },
        "Runtime": {
            "max_workers": "3",
            "transport_retries": "2",
            "node_retry_limit": "2",
            "stage1_retry_limit": "2",
            "review_section_retry_limit": "2",
            "validation_retry_limit": "1",
            "retry_base_delay_seconds": "30",
            "retry_max_delay_seconds": "120",
            "total_job_deadline_seconds": "0",
            "retain_checkpoints_after_completion": "false",
        },
        "Preprocess": {
            "enabled": "true",
            "cache_dir": "./output/_preprocess_cache",
            "parser_mode": "local",
            "primary_parser": "local",
            "fallback_parser": "local",
            "extractor_profile": "auto",
            "ocr_mode": "auto",
            "ocr_languages": "eng",
            "force_rebuild": "false",
            "use_markdown_as_stage1_input": "true",
            "retain_structured_output": "true",
            "retain_page_index": "true",
            "retain_diagnostics": "true",
            "enable_local_rag": "false",
            "rag_backend": "chroma",
        },
        "Styling": {
            "font_name": "Times New Roman",
            "font_size_body": "12",
            "font_size_heading1": "16",
            "font_size_heading2": "14",
        },
        "GUI": {
            "language": "zh-CN",
        },
        "Validator_API": {
            "api_key": "loaded_from_.env_file",
            "model": "deepseek-v4-pro",
            "api_base": PROVIDER_PRESETS["deepseek"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "thinking": "enabled",
            "reasoning_effort": "max",
            "max_context_tokens": "1000000",
            "max_output_tokens": "4096",
            "temperature": "0.3",
            "connect_timeout_seconds": "30",
            "read_timeout_seconds": "900",
            "total_timeout_seconds": "900",
            "first_token_timeout_seconds": "180",
            "transport_retries": "2",
            "reasoning_reserve_tokens": "2048",
            "safety_margin_tokens": "1024",
            "force_highest_reasoning": "true",
            "omit_temperature_when_reasoning": "true",
        },
        "Validation": {
            "stage1_enabled": "false",
            "review_enabled": "true",
            "repair_policy": "report_only",
            "evidence_resolver_enabled": "true",
            "visual_refs_enabled": "true",
            "review_drift_threshold": "0.3",
            "summary_drift_threshold": "0.2",
        },
        "Outline": {
            "candidate_count": "5",
            "relation_adjudication_enabled": "true",
            "structure_critique_enabled": "true",
            "coverage_critique_enabled": "true",
            "evidence_critique_enabled": "true",
            "require_explicit_adoption": "true",
            "technical_shard_target_tokens": "0",
            "allow_bibliometric_provider": "false",
        },
        "OutlineModels": {
            "outline_model": "Outline_API",
            "structure_critic_model": "Writer_API",
            "coverage_critic_model": "Primary_Reader_API",
            "evidence_critic_model": "Primary_Reader_API",
            "arbitrator_model": "Outline_API",
        },
        "OutlineCostControl": {
            "max_critique_models": "2",
            "max_summary_refs_per_prompt": "80",
            "max_outline_retry_count": "2",
        },
        "OutlineStability": {
            "mode": "smoke",
            "max_provider_calls": "24",
            "max_estimated_cost": "",
            "max_estimated_total_tokens": "5000000",
            "pricing_source": "",
            "pricing_provider": "",
            "pricing_model": "",
            "pricing_version": "",
            "pricing_effective_date": "",
            "estimated_cost_per_1k_tokens": "",
            "input_cost_per_1k_tokens": "",
            "output_cost_per_1k_tokens": "",
            "reasoning_cost_per_1k_tokens": "",
            "cache_read_cost_per_1k_tokens": "",
            "cache_write_cost_per_1k_tokens": "",
            "max_smoke_overhead_ratio": "2.0",
            "max_source_prompt_tokens": "0",
        },
        "OutlineQualityGate": dict(OUTLINE_QUALITY_GATE_DEFAULTS),
    }


def normalize_api_base(raw_value: str, provider: str = "custom") -> str:
    """Normalize user-provided API base URLs into root API endpoints."""

    value = (raw_value or "").strip()
    preset = PROVIDER_PRESETS.get(provider, PROVIDER_PRESETS["custom"])

    if not value:
        return preset.default_api_base.rstrip("/")

    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = f"https://{value}"

    value = re.sub(r"/chat/completions/?$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"/v1/chat/completions/?$", "/v1", value, flags=re.IGNORECASE)
    value = re.sub(r"/models/?$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"/v1/models/?$", "/v1", value, flags=re.IGNORECASE)
    value = value.rstrip("/")

    if preset.append_v1 and not re.search(r"/v\d+$", value, re.IGNORECASE):
        value = f"{value}/v1"

    return value.rstrip("/")


def ensure_config_sections(
    existing: Mapping[str, Mapping[str, str]] | None = None,
) -> Dict[str, Dict[str, str]]:
    """Merge current defaults with an existing config-like mapping.

    Unknown sections and keys are rejected instead of being silently carried
    forward into a new file.
    """

    merged = default_config_sections()
    if not existing:
        return merged

    key_errors = validate_config_keys(existing)
    if key_errors:
        raise ValueError("; ".join(key_errors))

    for section, values in existing.items():
        merged.setdefault(section, {})
        merged[section].update({key: str(value) for key, value in values.items()})

    # Outline API should inherit writer defaults if still blank.
    if not merged["Outline_API"].get("model"):
        merged["Outline_API"]["model"] = merged["Writer_API"].get("model", "")
    if not merged["Outline_API"].get("api_base"):
        merged["Outline_API"]["api_base"] = merged["Writer_API"].get("api_base", "")
    if not merged["Free_Mode_API"].get("model"):
        merged["Free_Mode_API"]["model"] = merged["Outline_API"].get("model", "")
    if not merged["Free_Mode_API"].get("api_base"):
        merged["Free_Mode_API"]["api_base"] = merged["Outline_API"].get("api_base", "")

    ApplicationSettings.from_mutable_config(merged)
    return merged


def read_env_file(env_path: str = ".env") -> Dict[str, str]:
    """Read a dotenv file without external dependencies."""

    data: Dict[str, str] = {}
    if not os.path.exists(env_path):
        return data

    with open(env_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()

    return data


def write_env_file(env_values: Mapping[str, str], env_path: str = ".env") -> None:
    """Persist environment variables to a dotenv file."""

    ordered_keys = [API_ENV_MAPPING[section] for section in API_ENV_MAPPING] + MINERU_ENV_KEYS
    existing = read_env_file(env_path)
    existing.update({key: str(value) for key, value in env_values.items()})

    lines = [
        "# Generated by auto-generate GUI setup",
        "",
    ]
    for key in ordered_keys:
        lines.append(f"{key}={existing.get(key, '')}")

    extra_keys = sorted(set(existing) - set(ordered_keys))
    if extra_keys:
        lines.append("")
        lines.append("# Existing custom variables")
        for key in extra_keys:
            lines.append(f"{key}={existing[key]}")

    with open(env_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines).rstrip() + "\n")


def write_config_file(
    sections: Mapping[str, Mapping[str, str]],
    config_path: str = "config.ini",
) -> None:
    """Write config sections to an ini file."""

    parser = configparser.ConfigParser()
    for section_name, values in sections.items():
        parser[section_name] = {key: str(value) for key, value in values.items()}

    with open(config_path, "w", encoding="utf-8") as handle:
        parser.write(handle)


def save_config_and_env(
    config_sections: Mapping[str, Mapping[str, str]],
    api_keys: Mapping[str, str],
    extra_env_values: Mapping[str, str] | None = None,
    config_path: str = "config.ini",
    env_path: str = ".env",
) -> None:
    """Persist both config.ini and .env in one call."""

    normalized = ensure_config_sections(config_sections)

    for section_name, env_key in API_ENV_MAPPING.items():
        normalized.setdefault(section_name, {})
        normalized[section_name]["api_key"] = "loaded_from_.env_file"
        if section_name == "Outline_API":
            normalized[section_name]["model"] = (
                normalized[section_name].get("model")
                or normalized["Writer_API"].get("model", "")
            )
            normalized[section_name]["api_base"] = (
                normalized[section_name].get("api_base")
                or normalized["Writer_API"].get("api_base", "")
            )
            for inherited_key in (
                "endpoint_type",
                "provider_family",
                "reasoning_effort",
                "reasoning_display",
                "force_highest_reasoning",
            ):
                normalized[section_name][inherited_key] = normalized[section_name].get(
                    inherited_key,
                    "",
                )
        if section_name == "Free_Mode_API":
            normalized[section_name]["model"] = (
                normalized[section_name].get("model")
                or normalized["Outline_API"].get("model", "")
            )
            normalized[section_name]["api_base"] = (
                normalized[section_name].get("api_base")
                or normalized["Outline_API"].get("api_base", "")
            )
            for inherited_key in (
                "endpoint_type",
                "provider_family",
                "reasoning_effort",
                "reasoning_display",
                "text_verbosity",
                "max_output_tokens",
                "force_highest_reasoning",
                "omit_temperature_when_reasoning",
            ):
                if not normalized[section_name].get(inherited_key):
                    normalized[section_name][inherited_key] = normalized["Outline_API"].get(inherited_key, "")

    write_config_file(normalized, config_path=config_path)
    env_payload = {
        API_ENV_MAPPING[section]: api_keys.get(section, "")
        for section in API_ENV_MAPPING
    }
    if extra_env_values:
        env_payload.update({key: str(value) for key, value in extra_env_values.items()})
    write_env_file(env_payload, env_path=env_path)


def normalize_for_save(config_sections: MutableMapping[str, Dict[str, str]]) -> None:
    """Normalize API base URLs in-place before writing config."""

    for section_name in API_ENV_MAPPING:
        section = config_sections.get(section_name)
        if not section:
            continue
        provider = section.get("provider", "custom")
        api_base = section.get("api_base", "")
        if not api_base and section_name in {"Outline_API", "Free_Mode_API"}:
            section["api_base"] = ""
        else:
            section["api_base"] = normalize_api_base(api_base, provider=provider)
        section.pop("provider", None)


def test_api_endpoint(api_key: str, api_base: str, model: str, proxy_mode: str = "environment") -> tuple[bool, str]:
    """Wrapper used by the GUI setup page."""

    normalized_base = normalize_api_base(api_base)
    return test_api_connection(api_key=api_key, api_base=normalized_base, model=model, proxy_mode=proxy_mode)

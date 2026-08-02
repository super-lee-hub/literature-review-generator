"""Configuration helpers for GUI setup and modernized defaults."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass
from typing import Dict, Mapping, MutableMapping

from outline.v2_config import OUTLINE_QUALITY_GATE_DEFAULTS
from config_validator import test_api_connection
from services.config_compat import apply_validation_compat_sections, remove_legacy_rate_limit_settings


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
            "force_highest_reasoning": "true",
            "omit_temperature_when_reasoning": "true",
        },
        "Backup_Reader_API": {
            "api_key": "loaded_from_.env_file",
            "model": "",
            "api_base": PROVIDER_PRESETS["videocaptioner"].default_api_base,
            "proxy_mode": "environment",
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
            "max_output_tokens": "32000",
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
            "force_highest_reasoning": "true",
        },
        "Free_Mode_API": {
            "api_key": "loaded_from_.env_file",
            "model": "",
            "api_base": "",
            "proxy_mode": "environment",
        },
        "Performance": {
            "max_workers": "3",
            "api_retry_attempts": "5",
            "enable_stage1_validation": "false",
            "enable_stage2_validation": "true",
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
        "Retry_Settings": {
            "max_retry_rounds": "2",
            "base_retry_delay": "30",
            "max_retry_delay": "120",
        },
        "Stage2_Retry": {
            "enabled": "true",
            "max_retry_rounds": "2",
            "base_retry_delay": "30",
            "max_retry_delay": "120",
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
        "API_Parameters": {
            "primary_max_tokens": "3000",
            "primary_temperature": "0.3",
            "timeout_seconds": "600",
            "backup_max_tokens": "8192",
            "backup_temperature": "0.3",
            "concept_max_tokens": "4000",
            "concept_temperature": "0.3",
            "writer_max_tokens": "32000",
            "writer_temperature": "0.5",
            "outline_max_tokens": "16000",
            "outline_temperature": "0.4",
            "free_mode_max_tokens": "6000",
            "free_mode_temperature": "0.4",
            "validator_max_tokens": "4096",
            "validator_temperature": "0.3",
            "validator_context_max_tokens": "1000000",
            "claims_max_tokens": "8192",
            "claims_temperature": "0.3",
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
            "force_highest_reasoning": "true",
            "omit_temperature_when_reasoning": "true",
        },
        "Validation": {
            "stage1_enabled": "false",
            "stage2_enabled": "true",
            "keep_checkpoints_after_completion": "false",
            "repair_policy": "report_only",
            "legacy_citation_policy": "report_only",
            "evidence_resolver_enabled": "true",
            "visual_refs_enabled": "true",
            "review_drift_threshold": "0.3",
            "summary_drift_threshold": "0.2",
        },
        "Outline": {
            "enable_outline_intelligence_v2": "false",
            "enable_literature_map": "true",
            "enable_synthesis_flow": "true",
            "candidate_count": "3",
            "enable_multi_model_critique": "true",
            "enable_coverage_audit": "true",
            "require_explicit_adopt": "true",
            "allow_bibliometric_provider": "false",
        },
        "OutlineModels": {
            "outline_model": "Outline_API",
            "structure_critic_model": "Writer_API",
            "coverage_critic_model": "Primary_Reader_API",
            "arbitrator_model": "Outline_API",
        },
        "OutlineCostControl": {
            "max_candidate_count": "3",
            "max_critique_models": "2",
            "max_summary_refs_per_prompt": "80",
            "max_outline_retry_count": "2",
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
    """Merge defaults with an existing config-like mapping."""

    merged = default_config_sections()
    if not existing:
        return apply_validation_compat_sections(merged)

    for section, values in existing.items():
        merged.setdefault(section, {})
        merged[section].update({key: str(value) for key, value in values.items()})

    remove_legacy_rate_limit_settings(merged)

    # Outline API should inherit writer defaults if still blank.
    if not merged["Outline_API"].get("model"):
        merged["Outline_API"]["model"] = merged["Writer_API"].get("model", "")
    if not merged["Outline_API"].get("api_base"):
        merged["Outline_API"]["api_base"] = merged["Writer_API"].get("api_base", "")
    if not merged["Free_Mode_API"].get("model"):
        merged["Free_Mode_API"]["model"] = merged["Outline_API"].get("model", "")
    if not merged["Free_Mode_API"].get("api_base"):
        merged["Free_Mode_API"]["api_base"] = merged["Outline_API"].get("api_base", "")

    return apply_validation_compat_sections(merged)


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

    remove_legacy_rate_limit_settings(config_sections)

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

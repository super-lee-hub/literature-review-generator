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
    "anthropic": ProviderPreset("Anthropic", "https://api.anthropic.com", append_v1=False),
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
            "model": "deepseek-v4-flash-vision-exp",
            "api_base": PROVIDER_PRESETS["deepseek"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "thinking": "enabled",
            "reasoning_effort": "high",
            "max_context_tokens": "1000000",
            "max_output_tokens": "32000",
            "temperature": "0.0",
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
            "model": "deepseek-v4-flash",
            "api_base": PROVIDER_PRESETS["deepseek"].default_api_base,
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "thinking": "enabled",
            "reasoning_effort": "high",
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
            "model": "gpt-5.6-sol",
            "api_base": "https://ai.saigou.work/v1",
            "proxy_mode": "environment",
            "endpoint_type": "responses",
            "provider_family": "openai_responses",
            "reasoning_effort": "high",
            "force_highest_reasoning": "true",
            "text_verbosity": "high",
            "max_context_tokens": "1000000",
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
            "model": "claude-opus-5",
            "api_base": "https://chat.178266.xyz",
            "proxy_mode": "environment",
            "endpoint_type": "anthropic",
            "provider_family": "anthropic",
            "anthropic_path": "/v1/messages",
            "anthropic_version": "2023-06-01",
            # "high" is the documented default. xhigh/max are supported but need
            # a very large max_tokens; that is a deliberate operator choice, not
            # something a shipped default should impose.
            #
            # force_highest_reasoning must stay false here. Left true, it
            # overrides the reasoning_effort above and silently requests the
            # model's top level -- "max" for Opus 5 -- so the shipped default
            # would read "high" while paying for, and being truncated by, "max".
            # An operator who wants max sets it explicitly and raises
            # max_output_tokens with it.
            "reasoning_effort": "high",
            "force_highest_reasoning": "false",
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
        },
        "Free_Mode_API": {
            "api_key": "loaded_from_.env_file",
            # Deliberately a different model from Outline_API: this section also
            # serves the coverage critique, so making it the same model as the
            # candidate generator would ship a self-reviewing default.
            "model": "deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "proxy_mode": "environment",
            "endpoint_type": "chat_completions",
            "provider_family": "deepseek",
            "reasoning_effort": "max",
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
            "parser_mode": "hybrid",
            "primary_parser": "mineru_remote",
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
            "model": "deepseek-v4-flash",
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
        "Queue": {
            "enabled": "false",
            "queue_file_path": "./output/_queue/queue.json",
            "max_concurrent_jobs": "1",
            "retry_attempts": "1",
        },
        "Stage1_Input": {
            "primary_reader_only": "false",
            "mode": "text_first",
            "send_extracted_text": "true",
            "send_selected_visuals": "true",
            "send_original_pdf": "never",
            "max_pdf_file_mb": "50",
            "force_pdf_file_input_for_provider": "false",
            "image_transport": "base64",
            "single_call_max_pages": "12",
            "visual_scan_batch_size": "8",
            "stage1_visual_scan_max_output_tokens": "16000",
            "stage1_synthesis_max_output_tokens": "64000",
            "stage1_length_retry_max_attempts": "1",
            "stage1_length_retry_ceiling_tokens": "128000",
            "stage1_request_timeout_seconds": "300",
            "stage1_semantic_retry_max_attempts": "1",
            "final_image_refs_max": "8",
            "require_complete_visual_coverage": "true",
            "max_request_image_bytes": "36000000",
            "max_single_image_bytes": "24000000",
        },
        "Stage1_Visual": {
            "enabled": "true",
            "selection_mode": "selective",
            "render_all_nonblank_pages": "false",
            "page_snapshot_soft_max": "4",
            "figure_crop_soft_max": "6",
            "table_crop_soft_max": "6",
            "formula_crop_soft_max": "4",
            "selected_visual_soft_total": "10",
            "selected_visual_hard_total": "16",
            "page_long_edge_px": "2200",
            "crop_long_edge_px": "2400",
            "page_max_pixels": "16000000",
            "crop_max_pixels": "16000000",
            "page_format": "jpeg",
            "page_jpeg_quality": "92",
            "crop_format": "png",
            "crop_padding_ratio": "0.04",
            "table_crop_enabled": "true",
            "formula_crop_enabled": "true",
            "max_visual_artifact_bytes": "24000000",
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
            "relation_adjudicator_model": "Free_Mode_API",
            "structure_critic_model": "Writer_API",
            "coverage_critic_model": "Free_Mode_API",
            "evidence_critic_model": "Writer_API",
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
            # Validated on live F1 (v19): bounded semantic repair + opaque
            # structural aliases are the production defaults now.
            "semantic_repair_enabled": "true",
            "opaque_alias_enabled": "true",
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

    # Migrate only the former shipped defaults.  A user-selected model is
    # preserved; text-first is the current Stage 1 mode, not permission to
    # overwrite arbitrary provider configuration.
    stage1_input = merged.setdefault("Stage1_Input", {})
    stage1_input.setdefault("mode", "text_first")
    legacy_primary_defaults = {"deepseek-v4-pro"}
    if (
        str(stage1_input.get("mode") or "").strip().lower() in {"vision_first", "text_first", "text_only"}
        and str(merged["Primary_Reader_API"].get("model") or "").strip().lower() in legacy_primary_defaults
    ):
        merged["Primary_Reader_API"]["model"] = "deepseek-v4-flash-vision-exp"
        merged["Primary_Reader_API"]["provider_family"] = "deepseek"
        merged["Primary_Reader_API"]["api_base"] = PROVIDER_PRESETS["deepseek"].default_api_base
    if str(merged["Backup_Reader_API"].get("model") or "").strip() == "":
        merged["Backup_Reader_API"].update(
            {
                "model": "deepseek-v4-flash",
                "api_base": PROVIDER_PRESETS["deepseek"].default_api_base,
                "provider_family": "deepseek",
                "thinking": "enabled",
                "reasoning_effort": "high",
            }
        )
    if str(merged["Validator_API"].get("model") or "").strip().lower() == "deepseek-v4-pro":
        merged["Validator_API"]["model"] = "deepseek-v4-flash"
        merged["Validator_API"]["provider_family"] = "deepseek"
    merged["Application"]["config_schema"] = str(CONFIG_SCHEMA_VERSION)

    # There is deliberately no Outline -> Writer / Free_Mode -> Outline
    # inheritance here any more.
    #
    # The old fallback copied only ``model`` and ``api_base``, leaving
    # ``endpoint_type``/``provider_family`` at the target section's own value.
    # Clearing Outline_API therefore produced a mongrel route: the Writer
    # gateway's address speaking the Anthropic Messages protocol. Under the
    # role-aware router an API section *is* the route authority, so a section
    # referenced by [OutlineModels] has to stand on its own. An incomplete one
    # is reported by ApplicationSettings.validate_outline_config() instead of
    # being quietly completed from a provider with a different wire format.

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
    # [Multimodal] remains readable for one migration cycle but is no longer
    # written.  Primary_Reader_API capability is the sole visual authority.
    normalized.pop("Multimodal", None)

    # No cross-section inheritance. These blocks used to copy part of another
    # section's identity onto Outline_API / Free_Mode_API (and in the Outline
    # case only assigned each key back to itself, so it inherited nothing at
    # all). Every section now persists exactly what its own route needs.
    for section_name, _env_key in API_ENV_MAPPING.items():
        normalized.setdefault(section_name, {})
        normalized[section_name]["api_key"] = "loaded_from_.env_file"

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
        # An empty base used to be left blank here on the assumption that a
        # fallback would fill it in later. There is no fallback any more, so a
        # routed section either carries its own base or is incomplete.
        section["api_base"] = normalize_api_base(api_base, provider=provider) if api_base else ""
        section.pop("provider", None)


def test_api_endpoint(
    api_key: str,
    api_base: str,
    model: str,
    proxy_mode: str = "environment",
    *,
    endpoint_type: str = "",
    provider_family: str = "",
    anthropic_path: str = "",
    anthropic_version: str = "",
) -> tuple[bool, str]:
    """Wrapper used by the GUI setup page."""

    normalized_endpoint = str(endpoint_type or "").strip().casefold().replace("-", "_")
    normalized_family = str(provider_family or "").strip().casefold().replace("-", "_")
    normalization_provider = (
        "anthropic"
        if normalized_endpoint == "anthropic" or normalized_family == "anthropic"
        else normalized_family if normalized_family in PROVIDER_PRESETS else "custom"
    )
    normalized_base = normalize_api_base(api_base, provider=normalization_provider)
    return test_api_connection(
        api_key=api_key,
        api_base=normalized_base,
        model=model,
        proxy_mode=proxy_mode,
        provider_family=normalized_family,
        endpoint_type=normalized_endpoint,
        anthropic_path=anthropic_path,
        anthropic_version=anthropic_version,
    )

import configparser
from pathlib import Path

from services.configuration_service import (
    default_config_sections,
    ensure_config_sections,
    normalize_api_base,
    write_env_file,
)
from services.settings import (
    CONFIG_KEYS,
    STAGE1_CONFIG_OWNERSHIP,
    ApplicationSettings,
    validate_config_keys,
)


def test_normalize_api_base_strips_chat_completion_suffix() -> None:
    assert normalize_api_base('https://example.com/v1/chat/completions') == 'https://example.com/v1'


def test_normalize_api_base_adds_v1_for_openai_compatible() -> None:
    assert normalize_api_base('https://api.example.com', provider='openai_compatible') == 'https://api.example.com/v1'


def test_ensure_config_sections_includes_outline_free_mode_and_preprocess() -> None:
    config = ensure_config_sections({})
    assert 'Outline_API' in config
    assert 'Free_Mode_API' in config
    assert 'Preprocess' in config
    assert 'Stage2_Retry' not in config
    assert 'Retry_Settings' not in config
    assert 'API_Parameters' not in config
    assert 'Validation' in config
    assert config['Preprocess']['parser_mode'] == 'hybrid'
    assert config['Preprocess']['primary_parser'] == 'mineru_remote'
    assert config['Preprocess']['use_markdown_as_stage1_input'] == 'true'
    assert config['Validation']['stage1_enabled'] == 'false'
    assert config['Runtime']['retain_checkpoints_after_completion'] == 'false'
    assert config['Validation']['repair_policy'] == 'report_only'
    assert 'legacy_citation_policy' not in config['Validation']
    assert config['Runtime']['transport_retries'] == '2'
    assert config['Primary_Reader_API']['proxy_mode'] == 'environment'
    assert config['Outline_API']['proxy_mode'] == 'environment'
    assert config['Validator_API']['proxy_mode'] == 'environment'
    assert config['Writer_API']['endpoint_type'] == 'responses'
    # The shipped Writer default is a third-party Responses gateway, not AIHubMix.
    assert config['Writer_API']['provider_family'] == 'openai_responses'
    assert config['Writer_API']['max_output_tokens'] == '32000'
    assert config['Writer_API']['text_verbosity'] == 'high'
    assert config['Writer_API']['max_output_tokens'] == '32000'
    assert config['Outline_API']['max_output_tokens'] == '16000'
    assert config['Primary_Reader_API']['thinking'] == 'enabled'
    assert config['Primary_Reader_API']['max_context_tokens'] == '1000000'
    # The Outline default is now a native Anthropic Messages endpoint; depth is
    # effort-driven there, so reasoning_display no longer applies.
    assert 'reasoning_display' not in config['Outline_API']
    assert config['Outline_API']['endpoint_type'] == 'anthropic'
    assert config['Validator_API']['reasoning_effort'] == 'max'
    assert config['OutlineQualityGate']['coverage_scope'] == 'full'
    assert config['OutlineQualityGate']['min_effective_sections'] == '3'


def test_legacy_strategy_policy_is_readable_but_removed_from_normalized_config() -> None:
    legacy = default_config_sections()
    legacy["Preprocess"]["strategy_policy"] = "local"

    normalized = ensure_config_sections(legacy)

    assert "strategy_policy" not in normalized["Preprocess"]


def test_production_outline_defaults_leave_pricing_unknown_but_keep_hard_token_ceiling() -> None:
    config = default_config_sections()
    settings = ApplicationSettings.from_mutable_config(config)
    stability = settings.outline_stability

    assert stability.pricing_source == ""
    assert stability.input_cost_per_1k_tokens is None
    assert stability.output_cost_per_1k_tokens is None
    assert stability.reasoning_cost_per_1k_tokens is None
    assert stability.cache_read_cost_per_1k_tokens is None
    assert stability.cache_write_cost_per_1k_tokens is None
    assert stability.max_estimated_cost is None
    assert stability.max_estimated_total_tokens > 0
    assert stability.max_source_prompt_tokens == 0


def test_write_env_file_allows_clearing_existing_keys(tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("LLM_PRIMARY_READER_API=old-key\nMINERU_API_TOKEN=old-token\n", encoding="utf-8")

    write_env_file(
        {
            "LLM_PRIMARY_READER_API": "",
            "MINERU_API_TOKEN": "",
        },
        env_path=str(env_path),
    )

    content = env_path.read_text(encoding="utf-8")
    assert "LLM_PRIMARY_READER_API=\n" in content
    assert "MINERU_API_TOKEN=\n" in content
    assert "old-key" not in content
    assert "old-token" not in content


def test_stage1_defaults_match_config_example_outside_secret_placeholders() -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(Path(__file__).resolve().parents[1] / "config.ini.example", encoding="utf-8")
    example = {section: dict(parser.items(section)) for section in parser.sections()}
    defaults = default_config_sections()
    secret_sections = {
        "Primary_Reader_API",
        "Backup_Reader_API",
        "Writer_API",
        "Outline_API",
        "Free_Mode_API",
        "Validator_API",
    }

    for section, values in defaults.items():
        expected = {
            key: value
            for key, value in values.items()
            if not (section in secret_sections and key == "api_key")
        }
        actual = {
            key: value
            for key, value in example[section].items()
            if not (section in secret_sections and key == "api_key")
        }
        assert actual == expected, f"config.ini.example drifted from default_config_sections in [{section}]"


def test_stage1_config_ownership_covers_every_accepted_key_and_rejects_removed_visual_knobs() -> None:
    for section in ("Stage1_Input", "Stage1_Visual"):
        assert set(CONFIG_KEYS[section]) <= set(STAGE1_CONFIG_OWNERSHIP[section])
        assert set(STAGE1_CONFIG_OWNERSHIP[section].values()) <= {
            "ACTIVE", "INVARIANT", "MIGRATION_ONLY", "REMOVE"
        }

    for key in ("max_visual_refs_per_paper", "visual_artifact_dir", "max_request_image_bytes", "max_single_image_bytes"):
        section = "Stage1_Visual"
        assert STAGE1_CONFIG_OWNERSHIP[section][key] == "REMOVE"
        assert validate_config_keys({section: {key: "1"}})


def test_stage1_migration_only_pdf_keys_are_readable_but_not_retained() -> None:
    config = {
        "Stage1_Input": {
            "pdf_required_for_formal_precision": "true",
            "formal_precision_text_only_policy": "block",
            "pdf_verifier_api": "Validator_API",
            "mode": "text_first",
            "image_transport": "base64",
        }
    }

    ApplicationSettings.from_mutable_config(config)

    assert set(config["Stage1_Input"]) == {"mode", "image_transport"}

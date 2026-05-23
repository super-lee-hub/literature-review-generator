from services.configuration_service import ensure_config_sections, normalize_api_base, write_env_file


def test_normalize_api_base_strips_chat_completion_suffix() -> None:
    assert normalize_api_base('https://example.com/v1/chat/completions') == 'https://example.com/v1'


def test_normalize_api_base_adds_v1_for_openai_compatible() -> None:
    assert normalize_api_base('https://api.example.com', provider='openai_compatible') == 'https://api.example.com/v1'


def test_ensure_config_sections_includes_outline_free_mode_and_preprocess() -> None:
    config = ensure_config_sections({})
    assert 'Outline_API' in config
    assert 'Free_Mode_API' in config
    assert 'Preprocess' in config
    assert 'Stage2_Retry' in config
    assert 'Validation' in config
    assert config['Preprocess']['parser_mode'] == 'local'
    assert config['Preprocess']['primary_parser'] == 'local'
    assert config['Preprocess']['use_markdown_as_stage1_input'] == 'true'
    assert config['Validation']['stage1_enabled'] == 'false'
    assert config['Validation']['stage2_enabled'] == 'true'
    assert config['Validation']['keep_checkpoints_after_completion'] == 'false'
    assert config['Validation']['repair_policy'] == 'report_only'
    assert config['Validation']['legacy_citation_policy'] == 'report_only'
    assert config['Performance']['enable_stage1_validation'] == 'false'
    assert config['Performance']['enable_stage2_validation'] == 'true'
    assert config['Primary_Reader_API']['proxy_mode'] == 'environment'
    assert config['Outline_API']['proxy_mode'] == 'environment'
    assert config['Validator_API']['proxy_mode'] == 'environment'
    assert config['Writer_API']['endpoint_type'] == 'responses'
    assert config['Writer_API']['provider_family'] == 'aihubmix_openai'
    assert config['Writer_API']['max_output_tokens'] == '32000'
    assert config['Writer_API']['text_verbosity'] == 'high'
    assert config['API_Parameters']['writer_max_tokens'] == '32000'
    assert config['API_Parameters']['outline_max_tokens'] == '16000'
    assert config['Primary_Reader_API']['thinking'] == 'enabled'
    assert config['Primary_Reader_API']['max_context_tokens'] == '1000000'
    assert config['Outline_API']['reasoning_display'] == 'summarized'
    assert config['Validator_API']['reasoning_effort'] == 'max'
    assert config['OutlineQualityGate']['coverage_scope'] == 'full'
    assert config['OutlineQualityGate']['min_effective_sections'] == '3'


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

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
    assert config['Performance']['enable_stage1_validation'] == 'false'
    assert config['Performance']['enable_stage2_validation'] == 'true'


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

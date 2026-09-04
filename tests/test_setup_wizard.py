import configparser

from services.configuration_service import read_env_file
from services.environment_service import RuntimeEnvironment
from services.settings import CONFIG_SCHEMA_VERSION
from setup_wizard import (
    _config_requires_explicit_migration,
    _load_existing_config_sections,
    _migrate_existing_config_explicitly,
    run_setup_wizard,
)


def test_run_setup_wizard_covers_extended_config_and_env(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.ini"
    env_path = tmp_path / ".env"

    config_path.write_text(
        "[Paths]\n"
        "zotero_report = old-report.html\n"
        "\n"
        "[Primary_Reader_API]\n"
        "model = old-model\n"
        "api_base = https://api.siliconflow.cn/v1\n",
        encoding="utf-8",
    )
    env_path.write_text("LLM_PRIMARY_READER_API=old-primary\n", encoding="utf-8")

    answers = iter(
        [
            "y",                                # explicitly migrate legacy config first
            # --- Paths ---
            "-",                               # zotero_report (clear)
            "D:/Zotero/storage",               # library_path
            "./custom-output",                 # output_path

            # --- Primary_Reader_API ---
            "",                                # provider (default: siliconflow)
            "deepseek-r1",                     # model
            "",                                # api_base (default)
            "",                                # api_key (keep existing)
            "",                                # endpoint_type (default: chat_completions)

            # --- Backup_Reader_API ---
            "openai_compatible",               # provider
            "gpt-4.1-mini",                    # model
            "https://api.example.com",         # api_base
            "backup-key",                      # api_key
            "",                                # endpoint_type (default: chat_completions)

            # --- Writer_API ---
            "",                                # provider (default: videocaptioner)
            "claude-writer",                   # model
            "",                                # api_base (default)
            "writer-key",                      # api_key
            "",                                # endpoint_type (default: responses)

            # --- Outline_API ---
            "",                                # provider (default: videocaptioner)
            "claude-opus-5",                   # model (complete route, no Writer fallback)
            "https://api.anthropic.com/v1",    # api_base (its own address)
            "outline-key",                     # api_key
            "anthropic",                       # endpoint_type
            "/v1/messages",                    # anthropic_path
            "2023-06-01",                      # anthropic_version
            "high",                            # Anthropic effort

            # --- Free_Mode_API ---
            "",                                # provider (default: videocaptioner)
            "planner-model",                   # model
            "http://localhost:11434",          # api_base
            "free-key",                        # api_key
            "",                                # endpoint_type (default: chat_completions)

            # --- OutlineModels (role routing) ---
            "Outline_API",                     # outline_model
            "Free_Mode_API",                   # relation_adjudicator_model
            "Writer_API",                       # structure_critic_model
            "Free_Mode_API",                   # coverage_critic_model
            "Writer_API",                       # evidence_critic_model
            "Outline_API",                      # arbitrator_model

            # --- Runtime ---
            "5",                                # max_workers
            "7",                                # transport_retries
            "2",                                # node_retry_limit
            "0",                                # stage1_retry_limit
            "3",                                # review_section_retry_limit
            "1",                                # validation_retry_limit
            "45",                               # retry_base_delay_seconds
            "180",                              # retry_max_delay_seconds
            "0",                                # total_job_deadline_seconds

            # --- Preprocess ---
            "y",                                # enabled
            "./cache",                          # cache_dir
            "hybrid",                           # parser_mode
            "mineru_remote",                    # primary_parser
            "local",                            # fallback_parser
            "pymupdf4llm",                      # extractor_profile
            "always",                           # ocr_mode
            "chi_sim+eng",                      # ocr_languages
            "y",                                # force_rebuild
            "n",                                # use_markdown_as_stage1_input
            "n",                                # retain_structured_output
            "y",                                # retain_page_index
            "n",                                # retain_diagnostics
            "y",                                # enable_local_rag
            "chroma",                           # rag_backend

            # --- MinerU ---
            "",                                 # 配置 MinerU 远程解析参数 (default: yes)
            "",                                 # MINERU_BASE_URL (default)
            "mineru-token",                     # MINERU_API_TOKEN
            "",                                 # MINERU_MODEL_VERSION
            "",                                 # MINERU_UPLOAD_ENDPOINT
            "",                                 # MINERU_POLL_ENDPOINT_TEMPLATES
            "4",                                # MINERU_POLL_INTERVAL_SECONDS
            "",                                 # MINERU_POLL_TIMEOUT_SECONDS
            "",                                 # MINERU_REQUEST_MAX_RETRIES
            "2.0",                              # MINERU_RETRY_BACKOFF_SECONDS
            "n",                                # ALLOW_LOCAL_PARSE_FALLBACK

            # --- Validation ---
            "y",                                # stage1_enabled
            "n",                                # review_enabled
            "",                                 # provider (default: openai)
            "gpt-4o-mini",                     # model
            "",                                 # api_base (default)
            "validator-key",                    # api_key
            "",                                 # endpoint_type (default: chat_completions)

            # --- GUI ---
            "en",                               # language

            # --- Styling ---
            "Calibri",                          # font_name
            "11",                               # font_size_body
            "15",                               # font_size_heading1
            "13",                               # font_size_heading2
        ]
    )

    monkeypatch.setattr("builtins.input", lambda _="": next(answers))
    monkeypatch.setattr(
        "setup_wizard.detect_runtime_environment",
        lambda: RuntimeEnvironment(
            kind="conda",
            name="auto-generate-gui",
            executable="python.exe",
            prefix="D:/envs/auto-generate-gui",
            is_conda=True,
            is_base_conda=False,
            is_virtual_env=False,
            is_isolated=True,
        ),
    )

    run_setup_wizard(config_path=str(config_path), env_path=str(env_path))

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")
    env_data = read_env_file(str(env_path))

    assert parser["Paths"]["zotero_report"] == ""
    assert parser["Paths"]["library_path"] == "D:/Zotero/storage"
    assert parser["Paths"]["output_path"] == "./custom-output"
    assert len(list(tmp_path.glob("config.ini.backup_before_*"))) == 1

    assert "Retry_Settings" not in parser
    assert "Stage2_Retry" not in parser
    assert parser["Runtime"]["max_workers"] == "5"
    assert parser["Runtime"]["transport_retries"] == "7"
    assert parser["Runtime"]["node_retry_limit"] == "2"
    assert parser["Runtime"]["stage1_retry_limit"] == "0"
    assert parser["Runtime"]["review_section_retry_limit"] == "3"
    assert parser["Runtime"]["validation_retry_limit"] == "1"
    assert parser["Runtime"]["retry_base_delay_seconds"] == "45"
    assert parser["Runtime"]["retry_max_delay_seconds"] == "180"
    assert parser["Runtime"]["total_job_deadline_seconds"] == "0"

    assert parser["Preprocess"]["parser_mode"] == "hybrid"
    assert parser["Preprocess"]["primary_parser"] == "mineru_remote"
    assert parser["Preprocess"]["fallback_parser"] == "local"
    assert parser["Preprocess"]["use_markdown_as_stage1_input"] == "false"
    assert parser["Preprocess"]["retain_structured_output"] == "false"
    assert parser["Preprocess"]["retain_page_index"] == "true"
    assert parser["Preprocess"]["retain_diagnostics"] == "false"
    assert parser["Preprocess"]["enable_local_rag"] == "true"
    assert parser["Preprocess"]["rag_backend"] == "chroma"

    assert parser["Validation"]["stage1_enabled"] == "true"
    assert parser["Validation"]["review_enabled"] == "false"
    assert parser["GUI"]["language"] == "en"
    assert parser["Styling"]["font_name"] == "Calibri"
    assert "API_Parameters" not in parser
    assert parser["Primary_Reader_API"]["max_output_tokens"] == "32000"
    assert parser["Writer_API"]["max_output_tokens"] == "32000"

    assert parser["Primary_Reader_API"]["model"] == "deepseek-r1"
    assert parser["Primary_Reader_API"]["endpoint_type"] == "chat_completions"

    assert parser["Outline_API"]["model"] == "claude-opus-5"
    # Outline_API is its own route authority: it must not have borrowed Writer's
    # gateway address or protocol. This is the mongrel-route regression guard.
    assert parser["Outline_API"]["api_base"] == "https://api.anthropic.com/v1"
    assert parser["Outline_API"]["endpoint_type"] == "anthropic"
    assert parser["Outline_API"]["provider_family"] == "anthropic"
    assert parser["Outline_API"]["api_base"] != parser["Writer_API"]["api_base"]
    assert parser["Outline_API"]["anthropic_path"] == "/v1/messages"
    assert parser["Outline_API"]["anthropic_version"] == "2023-06-01"
    assert parser["Outline_API"]["reasoning_effort"] == "high"

    assert parser["Free_Mode_API"]["model"] == "planner-model"
    assert parser["Free_Mode_API"]["api_base"] == "http://localhost:11434"
    assert parser["Free_Mode_API"]["endpoint_type"] == "chat_completions"

    assert parser["Validator_API"]["model"] == "gpt-4o-mini"
    assert parser["Validator_API"]["endpoint_type"] == "chat_completions"

    # Outline role routing should be persisted.
    assert parser["OutlineModels"]["outline_model"] == "Outline_API"
    assert parser["OutlineModels"]["relation_adjudicator_model"] == "Free_Mode_API"
    assert parser["OutlineModels"]["structure_critic_model"] == "Writer_API"
    assert parser["OutlineModels"]["coverage_critic_model"] == "Free_Mode_API"
    assert parser["OutlineModels"]["evidence_critic_model"] == "Writer_API"
    assert parser["OutlineModels"]["arbitrator_model"] == "Outline_API"

    assert env_data["LLM_PRIMARY_READER_API"] == "old-primary"
    assert env_data["LLM_BACKUP_READER_API"] == "backup-key"
    assert env_data["LLM_WRITER_API"] == "writer-key"
    assert env_data["LLM_OUTLINE_API"] == "outline-key"
    assert env_data["LLM_FREE_MODE_API"] == "free-key"
    assert env_data["LLM_VALIDATOR_API"] == "validator-key"
    assert env_data["MINERU_BASE_URL"] == "https://mineru.net/api/v4"
    assert env_data["MINERU_API_TOKEN"] == "mineru-token"
    assert env_data["MINERU_POLL_INTERVAL_SECONDS"] == "4"
    assert env_data["MINERU_RETRY_BACKOFF_SECONDS"] == "2.0"
    assert env_data["ALLOW_LOCAL_PARSE_FALLBACK"] == "false"

    assert list(answers) == []


def test_wizard_migrates_legacy_config_before_strict_load(tmp_path, monkeypatch) -> None:
    """D: a legacy config must migrate *before* the strict current-schema load.

    The wizard's ordering used to call the strict ``_load_existing_config_sections``
    first, so a config carrying ``[Retry_Settings]`` / ``[Stage2_Retry]`` /
    ``[API_Parameters]`` raised before the operator was ever offered a migration.
    The regression asserts the detect -> migrate -> load contract directly, using
    the same functions ``run_setup_wizard`` now calls in that order.
    """

    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Application]\n"
        "config_schema = 11\n"
        "\n"
        "[Primary_Reader_API]\n"
        "model = deepseek-v4-flash\n"
        "api_base = https://api.deepseek.com/v1\n"
        "api_key = legacy-primary\n"
        "\n"
        "[Retry_Settings]\n"
        "max_retry_rounds = 5\n"
        "base_retry_delay = 2\n"
        "\n"
        "[Stage2_Retry]\n"
        "stage2_retry_limit = 3\n"
        "\n"
        "[API_Parameters]\n"
        "primary_max_tokens = 8000\n"
        "validator_max_tokens = 9999\n"
        "timeout_seconds = 120\n"
        "temperature = 0.7\n",
        encoding="utf-8",
    )

    # Detect: the legacy config is recognised before any strict load.
    assert _config_requires_explicit_migration(str(config_path)) is True

    # Migrate: confirm the rewrite (the wizard gates this behind a yes/no prompt).
    monkeypatch.setattr("setup_wizard._prompt_yes_no", lambda *args, **kwargs: True)
    _migrate_existing_config_explicitly(str(config_path))

    # Strict load after migration must not raise and must drop legacy keys.
    sections = _load_existing_config_sections(str(config_path))

    # 1) a backup was taken before the rewrite
    backups = list(tmp_path.glob("config.ini.backup_before_*"))
    assert len(backups) == 1

    # 2) dead legacy sections are gone
    assert "Retry_Settings" not in sections
    assert "Stage2_Retry" not in sections
    assert "API_Parameters" not in sections

    # 3) relocated value landed in its provider section
    assert sections["Primary_Reader_API"]["max_output_tokens"] == "8000"

    # 4) the user's model choice was preserved, not promoted
    assert sections["Primary_Reader_API"]["model"] == "deepseek-v4-flash"

    # 5) timeout was relocated into the reader section
    assert sections["Primary_Reader_API"]["total_timeout_seconds"] == "120"

    # 6) a known-mapping key whose target section is missing (validator_max_tokens
    #    but no [Validator_API]) was preserved in the legacy block, not dropped (H#1)
    raw = config_path.read_text(encoding="utf-8")
    assert "# Validator_API.max_output_tokens = 9999" in raw

    # 7) schema stamped to current
    assert sections["Application"]["config_schema"] == str(CONFIG_SCHEMA_VERSION)


def test_migration_preserves_known_key_with_missing_target_section() -> None:
    """H#1: a known [API_Parameters] mapping whose home section is absent must be
    preserved in the legacy block, never silently dropped.
    """

    from services.config_migration import migrate_config_text

    text = (
        "[Application]\n"
        "config_schema = 11\n"
        "\n"
        "[Primary_Reader_API]\n"
        "model = deepseek-v4-flash\n"
        "api_base = https://api.deepseek.com/v1\n"
        "\n"
        "[API_Parameters]\n"
        "validator_max_tokens = 9999\n"
        "temperature = 0.7\n"
    )
    new_text, report = migrate_config_text(text)
    # No warning should claim the value was dropped.
    assert not any("were dropped" in warning for warning in report.warnings)
    # Both keys survive in the marked legacy block.
    assert "# Validator_API.max_output_tokens = 9999" in new_text
    assert "# temperature = 0.7" in new_text
    # And the dead [API_Parameters] section itself is gone.
    assert "[API_Parameters]" not in new_text

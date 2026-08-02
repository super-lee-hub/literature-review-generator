import configparser

from services.configuration_service import read_env_file
from services.environment_service import RuntimeEnvironment
from setup_wizard import run_setup_wizard


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
            "-",
            "D:/Zotero/storage",
            "./custom-output",
            "",
            "deepseek-r1",
            "",
            "",
            "openai_compatible",
            "gpt-4.1-mini",
            "https://api.example.com",
            "backup-key",
            "",
            "claude-writer",
            "",
            "writer-key",
            "",
            "-",
            "-",
            "",
            "",
            "planner-model",
            "http://localhost:11434",
            "free-key",
            "5",
            "7",
            "2",
            "0",
            "3",
            "45",
            "180",
            "y",
            "4",
            "60",
            "300",
            "y",
            "./cache",
            "hybrid",
            "mineru_remote",
            "local",
            "pymupdf4llm",
            "always",
            "chi_sim+eng",
            "y",
            "n",
            "n",
            "y",
            "n",
            "y",
            "chroma",
            "",
            "",
            "mineru-token",
            "",
            "",
            "",
            "4",
            "",
            "",
            "2.0",
            "n",
            "y",
            "n",
            "",
            "gpt-4o-mini",
            "",
            "validator-key",
            "en",
            "Calibri",
            "11",
            "15",
            "13",
            "900",
            "3500",
            "0.2",
            "",
            "",
            "",
            "",
            "9000",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
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

    assert parser["Retry_Settings"]["max_retry_rounds"] == "3"
    assert parser["Retry_Settings"]["base_retry_delay"] == "45"
    assert parser["Stage2_Retry"]["enabled"] == "true"
    assert parser["Stage2_Retry"]["max_retry_rounds"] == "4"

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
    assert parser["Runtime"]["node_retry_limit"] == "2"
    assert parser["Runtime"]["total_job_deadline_seconds"] == "0"
    assert parser["GUI"]["language"] == "en"
    assert parser["Styling"]["font_name"] == "Calibri"
    assert parser["API_Parameters"]["timeout_seconds"] == "900"
    assert parser["API_Parameters"]["primary_max_tokens"] == "3500"
    assert parser["API_Parameters"]["writer_max_tokens"] == "9000"

    assert parser["Primary_Reader_API"]["model"] == "deepseek-r1"
    assert parser["Outline_API"]["model"] == "claude-writer"
    assert parser["Outline_API"]["api_base"] == "https://aihubmix.com/v1"
    assert parser["Free_Mode_API"]["model"] == "planner-model"
    assert parser["Free_Mode_API"]["api_base"] == "http://localhost:11434"
    assert parser["Validator_API"]["model"] == "gpt-4o-mini"

    assert env_data["LLM_PRIMARY_READER_API"] == "old-primary"
    assert env_data["LLM_BACKUP_READER_API"] == "backup-key"
    assert env_data["LLM_WRITER_API"] == "writer-key"
    assert env_data["LLM_FREE_MODE_API"] == "free-key"
    assert env_data["LLM_VALIDATOR_API"] == "validator-key"
    assert env_data["MINERU_BASE_URL"] == "https://mineru.net/api/v4"
    assert env_data["MINERU_API_TOKEN"] == "mineru-token"
    assert env_data["MINERU_POLL_INTERVAL_SECONDS"] == "4"
    assert env_data["MINERU_RETRY_BACKOFF_SECONDS"] == "2.0"
    assert env_data["ALLOW_LOCAL_PARSE_FALLBACK"] == "false"

    assert list(answers) == []

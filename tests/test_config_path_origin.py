from __future__ import annotations

from pathlib import Path

import config_loader
import pytest
from preprocess.service import PreprocessManager
from services.credential_provenance import CredentialConflictError
from services.configuration_service import ensure_config_sections


def test_load_config_resolves_paths_from_config_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "spec" / "nested"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.ini"
    config = ensure_config_sections({})
    config["Paths"]["output_path"] = "relative-output"
    config["Primary_Reader_API"]["api_key"] = "test"
    config["Backup_Reader_API"]["api_key"] = "test"
    config["Writer_API"]["api_key"] = "test"
    lines: list[str] = []
    for section, values in config.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {value}" for key, value in values.items())
        lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    unrelated_cwd = tmp_path / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(config_loader, "validate_all_config", lambda _config: (True, []))
    monkeypatch.setattr(config_loader, "load_dotenv", lambda *args, **kwargs: False)

    loaded = config_loader.load_config(str(config_path))

    assert loaded["Paths"]["output_path"] == str((config_dir / "relative-output").resolve())


def test_load_config_resolves_preprocess_cache_and_queue_paths_from_config_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "spec"
    config_dir.mkdir()
    config_path = config_dir / "config.ini"
    config = ensure_config_sections({})
    config["Paths"]["output_path"] = "./relative-output"
    config["Preprocess"]["cache_dir"] = "./cache"
    config["Queue"]["queue_file_path"] = "./queue/queue.json"
    lines: list[str] = []
    for section, values in config.items():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {value}" for key, value in values.items())
        lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(config_loader, "validate_all_config", lambda _config, **_kwargs: (True, []))

    loaded = config_loader.load_config(str(config_path))

    assert loaded["Preprocess"]["cache_dir"] == str((config_dir / "cache").resolve())
    assert loaded["Queue"]["queue_file_path"] == str((config_dir / "queue/queue.json").resolve())


def test_load_config_reads_mineru_settings_from_config_directory_dotenv(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Application]\nconfig_schema=3\n[Paths]\noutput_path=output\n"
        "[Preprocess]\nenabled=true\n[Validation]\nstage1_enabled=false\nreview_enabled=false\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "MINERU_API_TOKEN=dotenv-token\nMINERU_RESPONSE_MAX_BYTES=1024\n"
        "MINERU_ALLOWED_URL_HOSTS=storage.example.com\nALLOW_LOCAL_PARSE_FALLBACK=false\n",
        encoding="utf-8",
    )
    for key in ("MINERU_API_TOKEN", "MINERU_RESPONSE_MAX_BYTES", "MINERU_ALLOWED_URL_HOSTS", "ALLOW_LOCAL_PARSE_FALLBACK"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config_loader, "validate_all_config", lambda _config, **_kwargs: (True, []))

    loaded = config_loader.load_config(str(config_path))

    assert loaded["Preprocess"]["mineru_api_token"] == "dotenv-token"
    assert loaded["Preprocess"]["mineru_response_max_bytes"] == "1024"
    assert loaded["Preprocess"]["mineru_allowed_url_hosts"] == "storage.example.com"
    assert loaded["Preprocess"]["allow_local_parse_fallback"] == "false"


def test_load_config_rejects_conflicting_mineru_process_and_dotenv_values(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[Application]\nconfig_schema=3\n[Paths]\noutput_path=output\n[Preprocess]\nenabled=true\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "MINERU_API_TOKEN=dotenv-only-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MINERU_API_TOKEN", "process-only-secret")
    monkeypatch.setattr(config_loader, "validate_all_config", lambda _config, **_kwargs: (True, []))

    with pytest.raises(CredentialConflictError) as caught:
        config_loader.load_config(str(config_path))

    message = str(caught.value)
    assert "MINERU_API_TOKEN" in message
    assert "dotenv-only-secret" not in message
    assert "process-only-secret" not in message


def test_preprocess_manager_keeps_formally_resolved_mineru_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MINERU_API_TOKEN", "stale-process-token")
    monkeypatch.setenv("MINERU_BASE_URL", "https://stale.example/api")

    manager = PreprocessManager(
        {
            "Paths": {"output_path": str(tmp_path)},
            "Preprocess": {
                "mineru_api_token": "resolved-config-token",
                "mineru_base_url": "https://resolved.example/api",
            },
        },
        preprocess_environment_resolved=True,
    )

    assert manager.mineru_api_token == "resolved-config-token"
    assert manager.mineru_base_url == "https://resolved.example/api"

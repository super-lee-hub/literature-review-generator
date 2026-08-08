from __future__ import annotations

from pathlib import Path

import config_loader
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

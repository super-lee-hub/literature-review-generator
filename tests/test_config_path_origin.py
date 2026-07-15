from __future__ import annotations

from pathlib import Path

import config_loader


def test_load_config_resolves_paths_from_config_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "spec" / "nested"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.ini"
    config_path.write_text(
        """
[Paths]
output_dir = relative-output
absolute_dir = C:/already-absolute

[Primary_Reader_API]
api_key = test

[Backup_Reader_API]
api_key = test

[Writer_API]
api_key = test
""".strip(),
        encoding="utf-8",
    )
    unrelated_cwd = tmp_path / "elsewhere"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setattr(config_loader, "validate_all_config", lambda _config: (True, []))
    monkeypatch.setattr(config_loader, "load_dotenv", lambda *args, **kwargs: False)

    loaded = config_loader.load_config(str(config_path))

    assert loaded["Paths"]["output_dir"] == str((config_dir / "relative-output").resolve())
    assert Path(loaded["Paths"]["absolute_dir"]).is_absolute()

from services.environment_service import (
    detect_runtime_environment,
    recommended_conda_activate_command,
    recommended_conda_create_command,
)


def test_detect_runtime_environment_identifies_dedicated_conda_env() -> None:
    runtime = detect_runtime_environment(
        environ={
            "CONDA_PREFIX": r"D:\Anaconda\envs\auto-generate-gui",
            "CONDA_DEFAULT_ENV": "auto-generate-gui",
        },
        executable=r"D:\Anaconda\envs\auto-generate-gui\python.exe",
        prefix=r"D:\Anaconda\envs\auto-generate-gui",
        base_prefix=r"D:\Anaconda",
    )

    assert runtime.kind == "conda"
    assert runtime.name == "auto-generate-gui"
    assert runtime.is_isolated is True
    assert runtime.needs_isolation_recommendation is False


def test_detect_runtime_environment_identifies_conda_base() -> None:
    runtime = detect_runtime_environment(
        environ={
            "CONDA_PREFIX": r"D:\Anaconda",
            "CONDA_DEFAULT_ENV": "base",
        },
        executable=r"D:\Anaconda\python.exe",
        prefix=r"D:\Anaconda",
        base_prefix=r"D:\Anaconda",
    )

    assert runtime.kind == "conda"
    assert runtime.is_base_conda is True
    assert runtime.is_isolated is False
    assert runtime.needs_isolation_recommendation is True


def test_detect_runtime_environment_identifies_virtualenv() -> None:
    runtime = detect_runtime_environment(
        environ={"VIRTUAL_ENV": r"D:\projects\.venv"},
        executable=r"D:\projects\.venv\Scripts\python.exe",
        prefix=r"D:\projects\.venv",
        base_prefix=r"C:\Python311",
    )

    assert runtime.kind == "venv"
    assert runtime.is_virtual_env is True
    assert runtime.is_isolated is True


def test_detect_runtime_environment_identifies_global_python() -> None:
    runtime = detect_runtime_environment(
        environ={},
        executable=r"C:\Python311\python.exe",
        prefix=r"C:\Python311",
        base_prefix=r"C:\Python311",
    )

    assert runtime.kind == "global"
    assert runtime.is_isolated is False
    assert runtime.needs_isolation_recommendation is True


def test_recommended_conda_commands_use_project_env_name() -> None:
    assert recommended_conda_create_command() == "conda create -n auto-generate-gui python=3.11"
    assert recommended_conda_activate_command() == "conda activate auto-generate-gui"

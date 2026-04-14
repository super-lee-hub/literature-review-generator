"""Runtime environment detection helpers for GUI and CLI entry points."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_CONDA_ENV_NAME = "auto-generate-gui"
DEFAULT_PYTHON_VERSION = "3.11"


@dataclass(frozen=True)
class RuntimeEnvironment:
    kind: str
    name: str
    executable: str
    prefix: str
    is_conda: bool
    is_base_conda: bool
    is_virtual_env: bool
    is_isolated: bool

    @property
    def display_name(self) -> str:
        if self.kind == "conda":
            return f"conda:{self.name}"
        if self.kind == "venv":
            return f"venv:{self.name}"
        return f"global:{self.name}"

    @property
    def needs_isolation_recommendation(self) -> bool:
        return self.is_base_conda or not self.is_isolated


def detect_runtime_environment(
    *,
    environ: Mapping[str, str] | None = None,
    executable: str | None = None,
    prefix: str | None = None,
    base_prefix: str | None = None,
) -> RuntimeEnvironment:
    env = dict(os.environ if environ is None else environ)
    current_executable = executable or sys.executable
    current_prefix = prefix or sys.prefix
    current_base_prefix = base_prefix or getattr(sys, "base_prefix", current_prefix)

    conda_prefix = str(env.get("CONDA_PREFIX", "") or "").strip()
    conda_name = str(env.get("CONDA_DEFAULT_ENV", "") or "").strip()
    virtual_env = str(env.get("VIRTUAL_ENV", "") or "").strip()

    if conda_prefix or conda_name:
        name = conda_name or Path(conda_prefix or current_prefix).name or "unknown"
        is_base_conda = name.lower() == "base"
        return RuntimeEnvironment(
        "conda",
        name,
        current_executable,
        current_prefix,
        True,
        is_base_conda,
        False,
        not is_base_conda,
    )

    if virtual_env or current_prefix != current_base_prefix:
        env_root = virtual_env or current_prefix
        env_name = Path(env_root).name or "venv"
        return RuntimeEnvironment(
        "venv",
        env_name,
        current_executable,
        current_prefix,
        False,
        False,
        True,
        True,
    )

    return RuntimeEnvironment(
        "global",
        Path(current_prefix).name or "python",
        current_executable,
        current_prefix,
        False,
        False,
        False,
        False,
    )


def recommended_conda_create_command(
    env_name: str = DEFAULT_CONDA_ENV_NAME,
    python_version: str = DEFAULT_PYTHON_VERSION,
) -> str:
    return f"conda create -n {env_name} python={python_version}"


def recommended_conda_activate_command(env_name: str = DEFAULT_CONDA_ENV_NAME) -> str:
    return f"conda activate {env_name}"

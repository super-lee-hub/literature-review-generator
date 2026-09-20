"""Explicit test-only runtime dependency injection.

Production entrypoints never infer offline authority from environment
variables.  The pytest harness may install an in-process dependency object
whose adapter guarantees that no external transport is available; subprocess
and normal user launches do not inherit this Python object.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeTestDependencies:
    allow_template_credentials: bool = True
    external_transport_disabled: bool = True

    def validate(self) -> None:
        if not self.external_transport_disabled:
            raise RuntimeError(
                "test runtime dependencies must disable every external transport"
            )
        if not isinstance(self.allow_template_credentials, bool):
            raise TypeError("test runtime allow_template_credentials must be a boolean")


_ACTIVE_TEST_DEPENDENCIES: RuntimeTestDependencies | None = None


def install_runtime_test_dependencies(dependencies: RuntimeTestDependencies) -> None:
    """Install explicit in-process test dependencies for the current process."""

    dependencies.validate()
    global _ACTIVE_TEST_DEPENDENCIES
    _ACTIVE_TEST_DEPENDENCIES = dependencies


def current_runtime_test_dependencies() -> RuntimeTestDependencies | None:
    return _ACTIVE_TEST_DEPENDENCIES


__all__ = [
    "RuntimeTestDependencies",
    "current_runtime_test_dependencies",
    "install_runtime_test_dependencies",
]

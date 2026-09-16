from __future__ import annotations

import ssl
from types import SimpleNamespace

from launch_gui import _configure_certifi_ssl_fallback


def test_certifi_ssl_fallback_is_opt_in_and_repairs_broken_default_context(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    def broken_default_context(*_args: object, **kwargs: object) -> object:
        calls.append(dict(kwargs))
        if "cafile" not in kwargs:
            raise ssl.SSLError("broken Windows certificate store")
        return object()

    monkeypatch.setenv("AUTO_GENERATE_GUI_CERTIFI_FALLBACK", "1")
    monkeypatch.setattr(ssl, "create_default_context", broken_default_context)
    monkeypatch.setitem(
        __import__("sys").modules,
        "certifi",
        SimpleNamespace(where=lambda: "C:/certifi/cacert.pem"),
    )

    _configure_certifi_ssl_fallback()
    ssl.create_default_context()

    assert calls == [{}, {"cafile": "C:/certifi/cacert.pem"}]

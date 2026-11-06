from __future__ import annotations

import pytest

from tests.test_run_first_scale_integration import _start_scale_server


def test_explicit_fixture_port_preserves_the_original_endpoint() -> None:
    initial, thread, base = _start_scale_server([])
    port = initial.server_address[1]
    initial.shutdown()
    initial.server_close()
    thread.join()

    reopened, reopened_thread, reopened_base = _start_scale_server([], bind_port=port)
    try:
        assert reopened_base == base
        assert reopened.server_address == ("127.0.0.1", port)
        assert reopened.calls == []
    finally:
        reopened.shutdown()
        reopened.server_close()
        reopened_thread.join()


def test_explicit_fixture_port_fails_if_another_listener_owns_it() -> None:
    server, thread, _base = _start_scale_server([])
    try:
        with pytest.raises(OSError):
            _start_scale_server([], bind_port=server.server_address[1])
        assert server.calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("port", [True, -1, 65536, "8105", 1.5])
def test_fixture_port_rejects_ambiguous_or_invalid_values(port) -> None:
    with pytest.raises(ValueError, match="loopback fixture port"):
        _start_scale_server([], bind_port=port)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Launcher for the local NiceGUI workspace."""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from services.environment_service import (
    detect_runtime_environment,
    recommended_conda_activate_command,
    recommended_conda_create_command,
)


def _can_bind(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_available_port(preferred_port: int, host: str = '127.0.0.1', attempts: int = 30) -> int:
    if _can_bind(host, preferred_port):
        return preferred_port

    for offset in range(1, attempts + 1):
        candidate = preferred_port + offset
        if _can_bind(host, candidate):
            return candidate

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _open_browser_when_ready(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    webbrowser.open(url, new=2, autoraise=True)
                    return
        except Exception:
            time.sleep(0.4)


def _print_environment_notice() -> None:
    runtime = detect_runtime_environment()
    print(f'Python runtime: {runtime.display_name}', file=sys.stderr)
    print(f'Interpreter: {runtime.executable}', file=sys.stderr)
    if runtime.needs_isolation_recommendation:
        print('Recommendation: use a dedicated conda environment to avoid package conflicts.', file=sys.stderr)
        print(f'  {recommended_conda_create_command()}', file=sys.stderr)
        print(f'  {recommended_conda_activate_command()}', file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description='Launch the local GUI for auto-generate.')
    parser.add_argument('--config', type=str, default='config.ini', help='Path to config.ini')
    parser.add_argument('--port', type=int, default=8098, help='Local GUI port')
    parser.add_argument('--reload', action='store_true', help='Enable NiceGUI auto reload for development')
    parser.add_argument('--no-show', action='store_true', help='Do not auto-open the browser')
    args = parser.parse_args()
    selected_port = _pick_available_port(args.port)
    launch_token = int(time.time())
    launch_url = f'http://127.0.0.1:{selected_port}/?launch={launch_token}'
    _print_environment_notice()
    if selected_port != args.port:
        print(f'Port {args.port} is unavailable; using {selected_port} instead.', file=sys.stderr)
    print(f'GUI target: {launch_url}', file=sys.stderr)
    if args.no_show:
        print('Browser auto-open is disabled; open the URL above manually.', file=sys.stderr)
    if args.reload:
        print('Development server is running in the foreground; keep this window open while you preview changes.', file=sys.stderr)
    try:
        if not args.no_show and not args.reload:
            threading.Thread(target=_open_browser_when_ready, args=(launch_url,), daemon=True).start()
        launch_gui(config_path=args.config, port=selected_port, reload=args.reload, show=False)
    except Exception as exc:
        print("GUI launch failed.", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if "NiceGUI is not installed" in str(exc):
            print("", file=sys.stderr)
            print("Install GUI dependencies with one of these commands:", file=sys.stderr)
            print("  python -m pip install -r requirements.txt", file=sys.stderr)
            print("  .\\venv\\Scripts\\python.exe -m pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(1) from exc


def launch_gui(
    config_path: str = 'config.ini',
    port: int = 8098,
    *,
    reload: bool = False,
    show: bool = True,
) -> None:
    from gui.app import BUILD_STAMP, launch_gui as _launch_gui

    print(f'GUI build: {BUILD_STAMP}', file=sys.stderr)

    _launch_gui(config_path=config_path, port=port, reload=reload, show=show)


if __name__ in {'__main__', '__mp_main__'}:
    main()

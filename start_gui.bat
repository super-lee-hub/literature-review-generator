@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_ENV=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_ENV%" (
    echo ERROR: Checkout-local GUI Python was not found: "%PYTHON_ENV%" 1>&2
    echo Create the Python 3.11 environment and install the documented GUI requirements before launching. 1>&2
    exit /b 2
)

set "PYTHON_EXE=%PYTHON_ENV%"
"%PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 (
    echo ERROR: start_gui.bat requires its checkout-local Python 3.11 interpreter. 1>&2
    exit /b 2
)

"%PYTHON_EXE%" "%~dp0launch_gui.py" --config "%~dp0config.ini" %* --port 8951 --strict-port
exit /b %errorlevel%

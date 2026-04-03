@echo off
setlocal
cd /d "%~dp0"

echo Closing any previous GUI processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process ^| Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'launch_gui.py' } ^| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
echo Releasing GUI ports 8098-8120 and 8950-8960...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue ^| Where-Object { ($_.LocalPort -ge 8098 -and $_.LocalPort -le 8120) -or ($_.LocalPort -ge 8950 -and $_.LocalPort -le 8960) } ^| Select-Object -ExpandProperty OwningProcess -Unique ^| ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 1 >nul

set "PREFERRED_ENV=auto-generate-gui"
set "CONDA_BAT="
set "PYTHON_EXE=python"
set "ACTIVE_ENV="

if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\miniconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%USERPROFILE%\anaconda3\condabin\conda.bat" set "CONDA_BAT=%USERPROFILE%\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%ProgramData%\miniconda3\condabin\conda.bat" set "CONDA_BAT=%ProgramData%\miniconda3\condabin\conda.bat"
if not defined CONDA_BAT if exist "%ProgramData%\anaconda3\condabin\conda.bat" set "CONDA_BAT=%ProgramData%\anaconda3\condabin\conda.bat"
if not defined CONDA_BAT (
    for %%I in (conda.bat) do set "CONDA_BAT=%%~$PATH:I"
)

if defined CONDA_BAT (
    call "%CONDA_BAT%" activate %PREFERRED_ENV% >nul 2>&1
    if not errorlevel 1 set "ACTIVE_ENV=%PREFERRED_ENV%"
)

if defined ACTIVE_ENV (
    echo Using conda environment %ACTIVE_ENV%
    python -c "import sys; print('Python:', sys.executable)"
    python -c "from gui.app import BUILD_STAMP; print('GUI build:', BUILD_STAMP)"
    python -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('nicegui') else 1)"
    if errorlevel 1 (
        echo.
        echo NiceGUI is not installed in conda environment %ACTIVE_ENV%.
        choice /C YN /M "Install dependencies from requirements.txt into %ACTIVE_ENV% now"
        if errorlevel 2 goto end
        python -m pip install -r requirements.txt
        if errorlevel 1 goto end
    )
    python launch_gui.py --port 8951
    goto end
)

echo Conda environment %PREFERRED_ENV% was not found or could not be activated.
if exist ".\venv\Scripts\python.exe" set "PYTHON_EXE=.\venv\Scripts\python.exe"
echo Using %PYTHON_EXE%
"%PYTHON_EXE%" -c "import sys; print('Python:', sys.executable)"
"%PYTHON_EXE%" -c "from gui.app import BUILD_STAMP; print('GUI build:', BUILD_STAMP)"
"%PYTHON_EXE%" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('nicegui') else 1)"
if errorlevel 1 (
    echo.
    echo NiceGUI is not installed in this Python environment.
    choice /C YN /M "Install dependencies from requirements.txt now"
    if errorlevel 2 goto end
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 goto end
)
"%PYTHON_EXE%" launch_gui.py --port 8951

:end
echo.
pause

@echo off
setlocal
cd /d "%~dp0"

echo Closing any previous GUI dev processes...
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
    echo Using conda environment %ACTIVE_ENV% in development mode
    python -c "import sys; print('Python:', sys.executable)"
    python -c "from gui.app import BUILD_STAMP; print('GUI build:', BUILD_STAMP)"
    echo Dev server will try port 8950 and automatically move to the next free port if needed.
    echo Development reload mode will NOT auto-open the browser, to avoid opening duplicate stale tabs.
    echo Use the LAST GUI target URL shown below.
    python launch_gui.py --reload --port 8950 --no-show
    goto end
)

echo Conda environment %PREFERRED_ENV% was not found or could not be activated.
if exist ".\venv\Scripts\python.exe" set "PYTHON_EXE=.\venv\Scripts\python.exe"
echo Using %PYTHON_EXE% in development mode
"%PYTHON_EXE%" -c "import sys; print('Python:', sys.executable)"
"%PYTHON_EXE%" -c "from gui.app import BUILD_STAMP; print('GUI build:', BUILD_STAMP)"
echo Dev server will try port 8950 and automatically move to the next free port if needed.
echo Development reload mode will NOT auto-open the browser, to avoid opening duplicate stale tabs.
echo Use the LAST GUI target URL shown below.
"%PYTHON_EXE%" launch_gui.py --reload --port 8950 --no-show

:end
echo.
pause

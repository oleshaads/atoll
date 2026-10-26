@echo off
setlocal
chcp 65001 >nul

set "APP_DIR=%LOCALAPPDATA%\atoll-agent"
set "VENV=%APP_DIR%\venv"
set "SCRIPT=%~dp0agent_console.py"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist "%VENV%\Scripts\python.exe" (
    echo [setup] creating virtual environment in %VENV%
    %PY% -m venv "%VENV%"
    if errorlevel 1 goto :err
)

"%VENV%\Scripts\python.exe" -c "import rich" >nul 2>nul
if errorlevel 1 (
    echo [setup] installing dependencies
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip --quiet
    "%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" --quiet
    if errorlevel 1 goto :err
)

"%VENV%\Scripts\python.exe" "%SCRIPT%" %*
exit /b %errorlevel%

:err
echo.
echo [error] setup failed. Make sure Python 3.9+ is installed (winget install Python.Python.3.12).
pause
exit /b 1

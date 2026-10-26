@echo off
setlocal
chcp 65001 >nul

set "APP_DIR=%LOCALAPPDATA%\atoll-agent"
set "VENV=%APP_DIR%\venv"
set "SCRIPT=%~dp0arena_hub.py"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"

if not exist "%VENV%\Scripts\python.exe" (
    echo [setup] создаю окружение
    %PY% -m venv "%VENV%"
    if errorlevel 1 goto :err
)

"%VENV%\Scripts\python.exe" -c "import rich" >nul 2>nul
if errorlevel 1 (
    echo [setup] ставлю зависимости
    "%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" --quiet
    if errorlevel 1 goto :err
)

"%VENV%\Scripts\python.exe" "%SCRIPT%" %*
exit /b %errorlevel%

:err
echo.
echo [error] не удалось подготовить окружение. Проверь, что установлен Python 3.9+.
pause
exit /b 1

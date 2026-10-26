@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python not found. Install it: winget install Python.Python.3.12
    echo Then run this file again.
    pause
    exit /b 1
)

%PY% app.py %*
if errorlevel 1 (
    echo.
    echo --- ошибка выше. Скопируй ее текст.
    pause
)

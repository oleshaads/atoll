@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem run-bridge.cmd — чат через GitHub: сообщения идут агенту Arena, ответ приходит,
rem когда агент прочитает канал. Нужен только для связи со мной.

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo Python not found. Install: winget install Python.Python.3.12
    pause
    exit /b 1
)

%PY% -c "import rich" >nul 2>nul
if errorlevel 1 %PY% -m pip install --quiet rich

%PY% app.py %*
if errorlevel 1 (
    echo.
    echo --- error above, copy the text ---
    pause
)

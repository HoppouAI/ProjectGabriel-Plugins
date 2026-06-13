@echo off
chcp 65001 > nul 2>&1
setlocal

title omnivoice_tts standalone server
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   Virtual environment not found. Run setup.bat first.
    echo.
    pause
    exit /b 1
)

:: keep our local bin first on PATH so the bundled uv is used
set "PATH=%~dp0bin;%PATH%"

call .venv\Scripts\activate.bat
echo   Starting omnivoice_tts server...
echo   Press Ctrl+C to stop.
echo.

python server.py %*

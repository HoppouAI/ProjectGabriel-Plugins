@echo off
REM Standalone omnivoice_tts server launcher (windows).
REM Creates a local .venv via `uv sync` on first run, then starts the
REM server. Any extra args you pass get forwarded to server.py.
REM
REM Usage:
REM   run.bat                          - use config.yml in this folder
REM   run.bat --port 9000              - override port
REM   run.bat --instruct "female, low" - override voice

setlocal enableextensions

cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: uv isn't on PATH. Install it from https://docs.astral.sh/uv/
    echo or with: powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    exit /b 2
)

if not exist .venv (
    echo first run, creating .venv and installing deps via uv sync ...
    uv sync
    if errorlevel 1 (
        echo.
        echo uv sync failed. you may need to install torch for your cuda
        echo version first, eg:
        echo   uv pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
        exit /b 1
    )
)

uv run server.py %*

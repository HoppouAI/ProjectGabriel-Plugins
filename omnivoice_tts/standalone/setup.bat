@echo off
chcp 65001 > nul 2>&1
setlocal enabledelayedexpansion

title omnivoice_tts standalone - Setup
cd /d "%~dp0"
cls

echo.
echo   ====================================================
echo        omnivoice_tts standalone - Setup
echo   ====================================================
echo.
echo   This will install uv, create a .venv, ask whether
echo   you want CPU or GPU inference, and pull all the
echo   python deps.
echo.

:: ---- Step 1: UV ----
echo   [1/4] UV package manager...

if not exist "bin" mkdir "bin"

if not exist "bin\uv.exe" (
    echo        Downloading UV...
    set "UV_INSTALL_DIR=%~dp0bin"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if not exist "bin\uv.exe" (
        echo   ERROR: UV download failed. Check your internet and try again.
        pause
        exit /b 1
    )
)
set "PATH=%~dp0bin;%PATH%"
echo        OK
echo.

:: ---- Step 2: venv ----
echo   [2/4] Creating Python 3.12 environment...

uv venv --python 3.12
if %errorlevel% neq 0 (
    echo.
    echo   ERROR: Could not create venv. UV will auto-download
    echo   Python 3.12 if you have it in your PATH or py launcher.
    echo   Otherwise install it from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
echo        OK
echo.

:: ---- Step 3: hardware ----
echo   [3/4] Hardware selection
echo.
echo        OmniVoice is a diffusion TTS. CPU works but is
echo        unusably slow for realtime chat. NVIDIA GPU
echo        STRONGLY recommended ^(6 GB+ vram^).
echo.
echo        1. CPU only ^(not recommended^)
echo        2. NVIDIA GPU ^(CUDA 12.8, 30/40/50 series^)
echo        3. NVIDIA GPU ^(CUDA 12.6, older drivers^)
echo.
choice /C 123 /M "        Choice"
set "GPU=%errorlevel%"
echo.

:: ---- Step 4: deps ----
echo   [4/4] Installing dependencies...
echo        This takes a few minutes the first time. Torch +
echo        the OmniVoice model deps are a chunky download.
echo.

uv sync
if %errorlevel% neq 0 (
    echo.
    echo   ERROR: Package install failed. See output above.
    pause
    exit /b 1
)

if "%GPU%"=="2" (
    echo.
    echo        Swapping in CUDA 12.8 PyTorch...
    uv pip install --index-url https://download.pytorch.org/whl/cu128 ^
        torch torchaudio --reinstall
    if %errorlevel% neq 0 (
        echo        WARNING: CUDA torch failed, CPU torch will be used.
        echo        Diffusion TTS will be VERY slow.
    ) else (
        echo        CUDA 12.8 PyTorch installed.
    )
)
if "%GPU%"=="3" (
    echo.
    echo        Swapping in CUDA 12.6 PyTorch...
    uv pip install --index-url https://download.pytorch.org/whl/cu126 ^
        torch torchaudio --reinstall
    if %errorlevel% neq 0 (
        echo        WARNING: CUDA torch failed, CPU torch will be used.
        echo        Diffusion TTS will be VERY slow.
    ) else (
        echo        CUDA 12.6 PyTorch installed.
    )
)

:: ---- config file ----
echo.
echo        Setting up config file...

if not exist "data" mkdir "data"

if not exist "config.yml" (
    if exist "config.example.yml" (
        copy /y "config.example.yml" "config.yml" > nul
        echo           config.yml ^(copied from example, tweak it before running^)
    )
) else (
    echo           config.yml already exists, leaving it alone.
)

:: ---- done ----
echo.
echo   ====================================================
echo        Setup complete
echo   ====================================================
echo.
echo   Edit config.yml if you want to pick a specific model,
echo   port, or voice. Defaults work for a quick test.
echo.
echo   Then run:  run.bat
echo.
pause
exit /b 0

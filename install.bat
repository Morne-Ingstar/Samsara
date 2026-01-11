@echo off
title Samsara Voice Dictation - Installer
cd /d "%~dp0"

echo ============================================
echo  Samsara Voice Dictation - Installer
echo ============================================
echo.

REM Check for Python
echo Checking for Python...
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Python not found!
    echo.
    echo Please install Python 3.10 or later from:
    echo   https://www.python.org/downloads/
    echo.
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

REM Check Python version
python -c "import sys; exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if %errorlevel% neq 0 (
    echo.
    echo WARNING: Python 3.10 or later is recommended.
    echo Your version may work but is not tested.
    echo.
    pause
)

echo Found Python:
python --version
echo.

REM Check if venv already exists
if exist "venv\Scripts\python.exe" (
    echo Virtual environment already exists.
    set /p REINSTALL="Reinstall dependencies? (y/N): "
    if /i not "%REINSTALL%"=="y" goto :skip_install
)

REM Create virtual environment
echo.
echo Creating virtual environment...
python -m venv venv
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to create virtual environment.
    echo Please ensure you have the 'venv' module installed.
    echo.
    pause
    exit /b 1
)

echo Virtual environment created.
echo.

REM Activate and install dependencies
echo Installing dependencies (this may take a few minutes)...
echo.
call venv\Scripts\activate.bat

REM Upgrade pip first
python -m pip install --upgrade pip --quiet

REM Install requirements
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Failed to install some dependencies.
    echo Please check your internet connection and try again.
    echo.
    pause
    exit /b 1
)

echo.
echo Dependencies installed successfully!
echo.

:skip_install

REM Quick test
echo Testing installation...
venv\Scripts\python.exe -c "import faster_whisper; import pystray; import sounddevice; print('All modules OK!')"
if %errorlevel% neq 0 (
    echo.
    echo WARNING: Some modules may not be installed correctly.
    echo The app may still work - try running it.
    echo.
)

REM Check for CUDA (optional)
echo.
echo Checking for GPU support...
venv\Scripts\python.exe -c "import torch; print(f'CUDA available: {torch.cuda.is_available()}')" 2>nul
if %errorlevel% neq 0 (
    echo GPU acceleration not available (CPU mode will be used).
    echo This is fine - transcription will just be a bit slower.
)

echo.
echo ============================================
echo  Installation Complete!
echo ============================================
echo.
echo To start Samsara:
echo   - Double-click: dist\Samsara.exe
echo   - Or run: venv\Scripts\python.exe dictation.py
echo.
echo First run will download the speech model (~150MB).
echo This only happens once.
echo.
echo For GPU acceleration (faster), install CUDA toolkit
echo and run: pip install torch
echo.

pause

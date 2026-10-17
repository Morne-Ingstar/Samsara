@echo off
setlocal
set "PROJECT_DIR=%~dp0.."
cd /d "%PROJECT_DIR%"
set INCLUDE_CUDA=1
set "PYTHON_ARGS=%SAMSARA_PYTHON_ARGS%"
if defined SAMSARA_PYTHON (
    set "PYTHON_EXE=%SAMSARA_PYTHON:"=%"
) else if exist "%PROJECT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
        set "PYTHON_ARGS=-3.11"
    ) else (
        where python >nul 2>nul
        if not errorlevel 1 set "PYTHON_EXE=python"
    )
)
if not defined PYTHON_EXE (
    echo Python was not found. Set SAMSARA_PYTHON to a Python 3.11 executable.
    exit /b 1
)
"%PYTHON_EXE%" %PYTHON_ARGS% -m PyInstaller --clean --noconfirm scripts\samsara.spec
exit /b %ERRORLEVEL%

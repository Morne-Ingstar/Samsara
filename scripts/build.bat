@echo off
title Build Samsara Launcher
cd /d "%~dp0"

echo ============================================
echo  Samsara Launcher Build Script
echo ============================================
echo.

REM Find Python - check local venv first, then conda, then PATH
set PYTHON_EXE=

REM Check local venv
if exist "venv\Scripts\python.exe" (
    set PYTHON_EXE=venv\Scripts\python.exe
    echo Found Python in local venv
    goto :found_python
)
if exist ".venv\Scripts\python.exe" (
    set PYTHON_EXE=.venv\Scripts\python.exe
    echo Found Python in local .venv
    goto :found_python
)

REM Check conda environments
for %%E in (sami samsara dictation) do (
    if exist "%USERPROFILE%\miniconda3\envs\%%E\python.exe" (
        set PYTHON_EXE=%USERPROFILE%\miniconda3\envs\%%E\python.exe
        echo Found Python in miniconda3\envs\%%E
        goto :found_python
    )
    if exist "%USERPROFILE%\anaconda3\envs\%%E\python.exe" (
        set PYTHON_EXE=%USERPROFILE%\anaconda3\envs\%%E\python.exe
        echo Found Python in anaconda3\envs\%%E
        goto :found_python
    )
)

REM Check PATH
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=*" %%i in ('where python') do (
        set PYTHON_EXE=%%i
        echo Found Python in PATH: %%i
        goto :found_python
    )
)

echo ERROR: Python not found!
echo.
echo Please ensure Python is installed and either:
echo   - Create a venv in this directory
echo   - Have a conda environment named 'sami' or 'samsara'
echo   - Add Python to your system PATH
echo.
pause
exit /b 1

:found_python
echo.
echo Killing any running instances...
taskkill /f /im python.exe 2>nul
taskkill /f /im pythonw.exe 2>nul
taskkill /f /im Samsara.exe 2>nul
timeout /t 1 /nobreak >nul

echo.
echo Building Samsara.exe...
"%PYTHON_EXE%" -m PyInstaller --onefile --windowed --name Samsara --clean --noconfirm samsara_launcher.py

if exist "dist\Samsara.exe" (
    echo.
    echo ============================================
    echo  Build successful!
    echo  Output: dist\Samsara.exe
    echo ============================================
    del /q Samsara.spec 2>nul
) else (
    echo.
    echo Build failed! Check the output above for errors.
)
echo.
pause

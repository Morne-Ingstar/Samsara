@echo off
rem Build Samsara via the canonical PyInstaller spec (scripts\samsara.spec,
rem same one build_release.bat uses) and immediately smoke-test the fresh
rem dist output with tools\frozen_smoke.py. One command = build + verify.
rem
rem Does NOT taskkill/close a running Samsara.exe. The release wrapper
rem (build_release.bat) runs the fail-closed process/worktree preflight first;
rem direct development use of this lower-level loop remains non-destructive.
rem This script never touches your real ~/.samsara profile; frozen_smoke.py
rem only ever launches against isolated temp profiles.
rem
rem Does NOT package/archive the build (that's build_release.bat's job) --
rem this is a build+verify loop, not a release step.

setlocal

set "PYTHON_ARGS=%SAMSARA_PYTHON_ARGS%"
if defined SAMSARA_PYTHON (
    set "PYTHON_EXE=%SAMSARA_PYTHON:"=%"
) else if exist "F:\envs\sami\python.exe" (
    set "PYTHON_EXE=F:\envs\sami\python.exe"
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
"%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>nul
if errorlevel 1 (
    echo Python could not run: %PYTHON_EXE% %PYTHON_ARGS%
    exit /b 1
)
cd /d "%~dp0.."
"%PYTHON_EXE%" %PYTHON_ARGS% tools\check_release_version.py
if errorlevel 1 exit /b 1

echo ============================================
echo  Build + smoke-test Samsara
echo ============================================
echo.

echo [1/3] Cleaning previous build...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build

echo.
echo [2/3] Running PyInstaller with scripts\samsara.spec...
"%PYTHON_EXE%" %PYTHON_ARGS% -m PyInstaller --clean --noconfirm scripts\samsara.spec

if not exist "dist\Samsara\Samsara.exe" (
    echo.
    echo Build FAILED -- dist\Samsara\Samsara.exe was not produced. Check output above.
    exit /b 1
)

echo.
echo Build successful: dist\Samsara\Samsara.exe

echo.
echo [3/3] Running frozen_smoke.py against the fresh build...
"%PYTHON_EXE%" %PYTHON_ARGS% tools\frozen_smoke.py dist\Samsara
set SMOKE_RESULT=%ERRORLEVEL%

exit /b %SMOKE_RESULT%

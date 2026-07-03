@echo off
rem Build Samsara via the canonical PyInstaller spec (scripts\samsara.spec,
rem same one build_release.bat uses) and immediately smoke-test the fresh
rem dist output with tools\frozen_smoke.py. One command = build + verify.
rem
rem Does NOT taskkill/close a running Samsara.exe for you (unlike
rem build_release.bat) -- if PyInstaller can't overwrite the EXE because an
rem instance is running, close it yourself and re-run. This script never
rem touches your real ~/.samsara profile; frozen_smoke.py only ever launches
rem against isolated temp profiles.
rem
rem Does NOT package/archive the build (that's build_release.bat's job) --
rem this is a build+verify loop, not a release step.

setlocal

set PYTHON=F:\envs\sami\python.exe
cd /d C:\Users\Morne\Projects\Samsara-dev

echo ============================================
echo  Build + smoke-test Samsara
echo ============================================
echo.

echo [1/3] Cleaning previous build...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build

echo.
echo [2/3] Running PyInstaller with scripts\samsara.spec...
"%PYTHON%" -m PyInstaller --clean --noconfirm scripts\samsara.spec

if not exist "dist\Samsara\Samsara.exe" (
    echo.
    echo Build FAILED -- dist\Samsara\Samsara.exe was not produced. Check output above.
    exit /b 1
)

echo.
echo Build successful: dist\Samsara\Samsara.exe

echo.
echo [3/3] Running frozen_smoke.py against the fresh build...
"%PYTHON%" tools\frozen_smoke.py dist\Samsara
set SMOKE_RESULT=%ERRORLEVEL%

exit /b %SMOKE_RESULT%

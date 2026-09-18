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

setlocal EnableExtensions EnableDelayedExpansion

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

set "CHECK_FAIL=0"
set "DIST_INTERNAL=dist\Samsara\_internal\openwakeword\resources\models"

echo.
echo [12] Checking packaged OWW model files are present in dist\_internal...
for %%M in (
    alexa_v0.1.onnx
    embedding_model.onnx
    hey_jarvis_v0.1.onnx
    hey_mycroft_v0.1.onnx
    hey_rhasspy_v0.1.onnx
    melspectrogram.onnx
    silero_vad.onnx
    timer_v0.1.onnx
    weather_v0.1.onnx
) do (
    if not exist "%DIST_INTERNAL%\%%M" (
        echo [CHECK-12-FAIL] missing "%DIST_INTERNAL%\%%M"
        set "CHECK_FAIL=1"
    )
)
if "%CHECK_FAIL%"=="1" (
    echo [CHECK-12] FAIL
    exit /b 1
)
echo [CHECK-12] PASS

echo.
echo [14] Running frozen import inventory against an isolated temp profile...
set "IMPORT_HOME=%TEMP%\samsara_import_%RANDOM%%RANDOM%"
set "IMPORT_LOG=%TEMP%\samsara_import_%RANDOM%%RANDOM%.log"
set "IMPORT_MARKER=%TEMP%\samsara_import_%RANDOM%%RANDOM%.ok"
mkdir "%IMPORT_HOME%" >nul 2>nul
set "SAMSARA_HOME_DIR=%IMPORT_HOME%"
set "SAMSARA_FROZEN_IMPORT_CHECK=1"
set "SAMSARA_FROZEN_IMPORT_MARKER=%IMPORT_MARKER%"
dist\Samsara\Samsara.exe > "%IMPORT_LOG%" 2>&1
set "IMPORT_RESULT=%ERRORLEVEL%"
set "SAMSARA_FROZEN_IMPORT_CHECK="
set "SAMSARA_FROZEN_IMPORT_MARKER="
set "SAMSARA_HOME_DIR="
rmdir /s /q "%IMPORT_HOME%"
if not "%IMPORT_RESULT%"=="0" (
    echo [CHECK-14] FAIL -- frozen import inventory exited %IMPORT_RESULT%
    if exist "%IMPORT_LOG%" type "%IMPORT_LOG%"
    exit /b %IMPORT_RESULT%
)
if not exist "%IMPORT_MARKER%" (
    echo [CHECK-14] FAIL -- frozen import inventory did not write its PASS marker
    if exist "%IMPORT_LOG%" type "%IMPORT_LOG%"
    exit /b 1
)
findstr /x /c:"PASS" "%IMPORT_MARKER%" >nul
if errorlevel 1 (
    echo [CHECK-14] FAIL -- frozen import inventory wrote a non-PASS marker
    type "%IMPORT_MARKER%"
    if exist "%IMPORT_LOG%" type "%IMPORT_LOG%"
    exit /b 1
)
del "%IMPORT_MARKER%" >nul 2>nul
echo [CHECK-14] PASS -- every Samsara/plugin/Qt inventory module imported

echo.
echo [15] Proving the core refuses gesture control without its component...
set "GESTURE_HOME=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%"
set "GESTURE_LOG=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%.log"
set "GESTURE_MARKER=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%.ok"
mkdir "%GESTURE_HOME%" >nul 2>nul
set "SAMSARA_HOME_DIR=%GESTURE_HOME%"
set "SAMSARA_GESTURE_COMPONENT_CHECK=missing"
set "SAMSARA_FROZEN_IMPORT_MARKER=%GESTURE_MARKER%"
dist\Samsara\Samsara.exe > "%GESTURE_LOG%" 2>&1
set "GESTURE_RESULT=%ERRORLEVEL%"
set "SAMSARA_GESTURE_COMPONENT_CHECK="
set "SAMSARA_FROZEN_IMPORT_MARKER="
set "SAMSARA_HOME_DIR="
rmdir /s /q "%GESTURE_HOME%"
if not "%GESTURE_RESULT%"=="0" (
    echo [CHECK-15] FAIL -- missing-component check exited %GESTURE_RESULT%
    if exist "%GESTURE_LOG%" type "%GESTURE_LOG%"
    exit /b %GESTURE_RESULT%
)
findstr /x /c:"PASS" "%GESTURE_MARKER%" >nul
if errorlevel 1 (
    echo [CHECK-15] FAIL -- missing-component check did not write PASS
    if exist "%GESTURE_MARKER%" type "%GESTURE_MARKER%"
    if exist "%GESTURE_LOG%" type "%GESTURE_LOG%"
    exit /b 1
)
del "%GESTURE_MARKER%" >nul 2>nul
echo [CHECK-15] PASS -- missing component is handled without app startup

echo.
echo [16] Proving the installed gesture component imports in the frozen app...
set "GESTURE_ZIP="
for %%F in (dist\Samsara-GestureControl-*.zip) do set "GESTURE_ZIP=%%~fF"
if not defined GESTURE_ZIP (
    echo [CHECK-16] FAIL -- gesture component archive was not built
    exit /b 1
)
set "GESTURE_APP=%TEMP%\samsara_gesture_app_%RANDOM%%RANDOM%"
set "GESTURE_HOME=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%"
set "GESTURE_LOG=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%.log"
set "GESTURE_MARKER=%TEMP%\samsara_gesture_%RANDOM%%RANDOM%.ok"
"%PYTHON_EXE%" %PYTHON_ARGS% -c "import shutil,sys,zipfile; from pathlib import Path; shutil.copytree(sys.argv[1], sys.argv[2]); zipfile.ZipFile(sys.argv[3]).extractall(Path(sys.argv[2]) / '_internal')" "dist\Samsara" "%GESTURE_APP%" "%GESTURE_ZIP%"
if errorlevel 1 (
    echo [CHECK-16] FAIL -- could not prepare the isolated component install
    rmdir /s /q "%GESTURE_APP%"
    exit /b 1
)
mkdir "%GESTURE_HOME%" >nul 2>nul
set "SAMSARA_HOME_DIR=%GESTURE_HOME%"
set "SAMSARA_GESTURE_COMPONENT_CHECK=installed"
set "SAMSARA_FROZEN_IMPORT_MARKER=%GESTURE_MARKER%"
"%GESTURE_APP%\Samsara.exe" > "%GESTURE_LOG%" 2>&1
set "GESTURE_RESULT=%ERRORLEVEL%"
set "SAMSARA_GESTURE_COMPONENT_CHECK="
set "SAMSARA_FROZEN_IMPORT_MARKER="
set "SAMSARA_HOME_DIR="
rmdir /s /q "%GESTURE_HOME%"
rmdir /s /q "%GESTURE_APP%"
if not "%GESTURE_RESULT%"=="0" (
    echo [CHECK-16] FAIL -- installed-component check exited %GESTURE_RESULT%
    if exist "%GESTURE_LOG%" type "%GESTURE_LOG%"
    exit /b %GESTURE_RESULT%
)
findstr /x /c:"PASS" "%GESTURE_MARKER%" >nul
if errorlevel 1 (
    echo [CHECK-16] FAIL -- installed-component check did not write PASS
    if exist "%GESTURE_MARKER%" type "%GESTURE_MARKER%"
    if exist "%GESTURE_LOG%" type "%GESTURE_LOG%"
    exit /b 1
)
del "%GESTURE_MARKER%" >nul 2>nul
echo [CHECK-16] PASS -- frozen component import and gesture loop start

echo.
echo [3/3] Running frozen_smoke.py against the fresh build...
set "SMOKE_LOG=%TEMP%\samsara_smoke_%RANDOM%.log"
"%PYTHON_EXE%" %PYTHON_ARGS% tools\frozen_smoke.py dist\Samsara > "%SMOKE_LOG%" 2>&1
set "SMOKE_RESULT=%ERRORLEVEL%"

echo [13] Scanning frozen smoke log for OWW model-load failures...
if not exist "%SMOKE_LOG%" (
    echo [CHECK-13] FAIL -- missing smoke log "%SMOKE_LOG%"
    exit /b 1
)
findstr /c:"[OWW] Failed to load model" "%SMOKE_LOG%" >nul
if not errorlevel 1 (
    echo [CHECK-13-FAIL] OWW model load failure found in smoke log
    set "CHECK_FAIL=1"
) else (
    echo [CHECK-13] PASS -- no OWW model load failure in smoke log
)
if "%CHECK_FAIL%"=="1" exit /b 1

if "%SMOKE_RESULT%"=="0" (
    echo [3/3] frozen_smoke.py: PASS
) else (
    echo [3/3] frozen_smoke.py: FAIL (exit %SMOKE_RESULT%)
    exit /b %SMOKE_RESULT%
)

if not "%CHECK_FAIL%"=="0" exit /b 1
echo.
echo [SIZE] Fresh build report:
"%PYTHON_EXE%" %PYTHON_ARGS% tools\size_report.py dist\Samsara
exit /b %SMOKE_RESULT%

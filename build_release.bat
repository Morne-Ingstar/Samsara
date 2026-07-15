@echo off
setlocal
title Building Samsara v0.22.0
cd /d "%~dp0"

set VERSION=0.22.0
set INCLUDE_CUDA=0
set ARCHIVE=dist\Samsara-Windows-v%VERSION%.7z
set "PYTHON_ARGS="
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

echo ============================================
echo  Building Samsara v%VERSION%
echo ============================================
echo.

echo [1/3] Verifying release identity and that Samsara is closed...
if not defined PYTHON_EXE (
    echo Python was not found. Set SAMSARA_PYTHON to a Python 3.11 executable.
    exit /b 1
)
"%PYTHON_EXE%" %PYTHON_ARGS% --version >nul 2>nul
if errorlevel 1 (
    echo Python could not run: %PYTHON_EXE% %PYTHON_ARGS%
    exit /b 1
)
"%PYTHON_EXE%" %PYTHON_ARGS% tools\check_release_version.py --expected "%VERSION%"
if errorlevel 1 (
    echo Release version identity check FAILED. No build was started.
    exit /b 1
)
"%PYTHON_EXE%" %PYTHON_ARGS% tools\release_preflight.py
if errorlevel 1 (
    echo Resolve the release preflight failure above and retry.
    exit /b 1
)

echo.
echo [2/3] Building the CPU package and running frozen smoke checks...
set "SAMSARA_PYTHON=%PYTHON_EXE%"
set "SAMSARA_PYTHON_ARGS=%PYTHON_ARGS%"
call tools\build_and_smoke.cmd
if errorlevel 1 (
    echo.
    echo Build or frozen smoke verification FAILED. No release archive was created.
    exit /b 1
)

if not exist "dist\Samsara\Samsara.exe" (
    echo.
    echo Verified build output is missing: dist\Samsara\Samsara.exe
    exit /b 1
)

echo.
echo Build successful:
dir dist\Samsara\Samsara.exe | findstr "Samsara"

echo.
echo [3/3] Archiving the verified dist\Samsara output...
where 7z.exe >nul 2>nul
if errorlevel 1 (
    echo 7z.exe was not found on PATH. No release archive was created.
    exit /b 1
)
if exist "%ARCHIVE%" (
    del /q "%ARCHIVE%"
    if exist "%ARCHIVE%" (
        echo Existing archive could not be removed: %ARCHIVE%
        exit /b 1
    )
)
7z a -mx=5 "%ARCHIVE%" "dist\Samsara\*" -r
if errorlevel 1 (
    echo.
    echo 7z compression failed.
    exit /b 1
)

if not exist "%ARCHIVE%" (
    echo.
    echo 7z reported success but did not create %ARCHIVE%.
    exit /b 1
)

echo.
echo Done. Archive details:
dir "%ARCHIVE%" | findstr "Samsara"
echo.
powershell -Command "$size = (Get-Item '%ARCHIVE%').Length; Write-Host ('Compressed: {0:N1} MB' -f ($size/1MB))"
echo.
echo Ready to upload to GitHub release v%VERSION%.
exit /b 0

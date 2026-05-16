@echo off
title Building Samsara v0.10.0
cd /d C:\Users\Morne\Projects\Samsara-dev

set VERSION=0.10.0
set ARCHIVE=dist\Samsara-Windows-v%VERSION%.7z

echo ============================================
echo  Building Samsara v%VERSION%
echo ============================================
echo.

echo [1/4] Killing any running instances...
taskkill /f /im Samsara.exe 2>nul
timeout /t 2 /nobreak >nul

echo.
echo [1.5/4] Cleaning previous build...
if exist "dist" rmdir /s /q dist
if exist "build" rmdir /s /q build

echo.
echo [2/4] Running PyInstaller with spec file...
python -m PyInstaller --clean --noconfirm scripts\samsara.spec

if not exist "dist\Samsara\Samsara.exe" (
    echo.
    echo Build FAILED. Check output above.
    pause
    exit /b 1
)

echo.
echo Build successful:
dir dist\Samsara\Samsara.exe | findstr "Samsara"

echo.
echo [3/4] Compressing dist\Samsara -> %ARCHIVE%
if exist "%ARCHIVE%" del "%ARCHIVE%"
7z a -mx=5 "%ARCHIVE%" "dist\Samsara\*" -r

if not exist "%ARCHIVE%" (
    echo.
    echo 7z compression failed.
    pause
    exit /b 1
)

echo.
echo [4/4] Done. Archive details:
dir "%ARCHIVE%" | findstr "Samsara"
echo.
powershell -Command "$size = (Get-Item '%ARCHIVE%').Length; Write-Host ('Compressed: {0:N1} MB' -f ($size/1MB))"
echo.
echo Ready to upload to GitHub release v%VERSION%.
echo.
pause

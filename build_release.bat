@echo off
title Building Samsara v0.9.2
cd /d C:\Users\Morne\Projects\Samsara-dev

echo ============================================
echo  Building Samsara v0.9.2
echo ============================================
echo.

echo Killing any running instances...
taskkill /f /im Samsara.exe 2>nul
timeout /t 1 /nobreak >nul

echo.
echo Running PyInstaller with spec file...
python -m PyInstaller --clean --noconfirm scripts\samsara.spec

if exist "dist\Samsara\Samsara.exe" (
    echo.
    echo ============================================
    echo  Build successful!
    echo  Output: dist\Samsara\
    echo ============================================
    dir dist\Samsara\Samsara.exe
) else (
    echo.
    echo Build FAILED! Check output above.
)
echo.
pause

@echo off
title Building Samsara v0.9.4
cd /d C:\Users\Morne\Projects\Samsara-dev

echo ============================================
echo  Building Samsara v0.9.4
echo ============================================
echo.

echo Killing any running instances...
taskkill /f /im Samsara.exe 2>nul
timeout /t 2 /nobreak >nul

echo.
echo Running PyInstaller with spec file...
python -m PyInstaller --clean --noconfirm scripts\samsara.spec

if exist "dist\Samsara\Samsara.exe" (
    echo.
    echo ============================================
    echo  Build successful!
    echo ============================================
    dir dist\Samsara\Samsara.exe
    
    echo.
    echo Compressing with 7z...
    if exist "dist\Samsara-Windows-v0.9.4.7z" del "dist\Samsara-Windows-v0.9.4.7z"
    7z a -mx=5 "dist\Samsara-Windows-v0.9.4.7z" "dist\Samsara\*" -r
    
    if exist "dist\Samsara-Windows-v0.9.4.7z" (
        echo.
        echo Archive created:
        dir "dist\Samsara-Windows-v0.9.4.7z"
        echo.
        echo Ready to upload to GitHub release.
    ) else (
        echo 7z compression failed
    )
) else (
    echo.
    echo Build FAILED! Check output above.
)
echo.

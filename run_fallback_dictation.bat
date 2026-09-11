@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
title Samsara Fallback Dictation

pushd "%PROJECT_DIR%"
"F:\envs\sami\python.exe" "%PROJECT_DIR%\tools\fallback_dictation.py"
if errorlevel 1 pause
popd

endlocal

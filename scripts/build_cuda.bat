@echo off
set INCLUDE_CUDA=1
cd /d C:\Users\Morne\Projects\Samsara-dev
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
F:\envs\sami\python.exe -m PyInstaller --clean --noconfirm scripts\samsara.spec

@echo off
REM Launch Samsara with its correct interpreter, bypassing whatever
REM bare `python` resolves to on PATH (Hermes venv / Python313 / etc.).
cd /d C:\Users\Morne\Projects\Samsara-dev
F:\envs\sami\python.exe dictation.py %*

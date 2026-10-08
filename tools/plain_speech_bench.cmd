@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
set "BENCH_PYTHON=F:\envs\sami\python.exe"
if not exist "%BENCH_PYTHON%" set "BENCH_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%BENCH_PYTHON%" (
  echo This benchmark needs the development Python environment and Samsara source.
  echo The packaged laptop build needs a benchmark entry point in a later build.
  pause
  exit /b 2
)
"%BENCH_PYTHON%" "%~dp0plain_speech_bench.py" --output-dir "%USERPROFILE%\Documents\Samsara-bench"
set "BENCH_EXIT=%ERRORLEVEL%"
if exist "%USERPROFILE%\Documents\Samsara-bench" start "" explorer.exe "%USERPROFILE%\Documents\Samsara-bench"
if not "%BENCH_EXIT%"=="0" pause
exit /b %BENCH_EXIT%

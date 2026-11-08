@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0..\.." || exit /b 1
rem Set SAMSARA_CORPUS_PYTHON to an interpreter path without surrounding quotes.
if not defined SAMSARA_CORPUS_PYTHON if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\pythonw.exe" set "SAMSARA_CORPUS_PYTHON=%VIRTUAL_ENV%\Scripts\pythonw.exe"
if not defined SAMSARA_CORPUS_PYTHON if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\pythonw.exe" set "SAMSARA_CORPUS_PYTHON=%CONDA_PREFIX%\pythonw.exe"
if not defined SAMSARA_CORPUS_PYTHON set "SAMSARA_CORPUS_PYTHON=pythonw.exe"
rem Preserve the existing corpus location; any external folder can override it.
if not defined SAMSARA_VOICE_ROOT set "SAMSARA_VOICE_ROOT=D:\SamsaraCorpus"
start "" "%SAMSARA_CORPUS_PYTHON%" -m tools.voice_corpus record --corpus "%SAMSARA_VOICE_ROOT%"

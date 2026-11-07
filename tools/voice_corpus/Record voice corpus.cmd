@echo off
cd /d "%~dp0..\.."
if defined SAMSARA_CORPUS_PYTHON (
  start "" "%SAMSARA_CORPUS_PYTHON%" -m tools.voice_corpus record
) else if exist "F:\envs\sami\pythonw.exe" (
  start "" "F:\envs\sami\pythonw.exe" -m tools.voice_corpus record
) else (
  pythonw -m tools.voice_corpus record
)

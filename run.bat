@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing venv. See README Setup.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "gaze_switch.py" %*
if errorlevel 1 pause

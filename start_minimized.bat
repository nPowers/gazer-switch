@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "gaze_switch.py" --minimized
) else (
  start "" /min ".venv\Scripts\python.exe" "gaze_switch.py" --minimized
)
